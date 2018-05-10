import os
import tensorflow as tf
import tensorlayer as tl
from tensorlayer.layers import \
    Layer, \
    InputLayer, Conv1d, MaxPool1d, \
    RNNLayer, DropoutLayer, DenseLayer, \
    LambdaLayer, ReshapeLayer, ConcatLayer, \
    Conv2d, MaxPool2d, FlattenLayer, \
    DeConv2d, BatchNormLayer, EmbeddingInputlayer, \
    Seq2Seq, retrieve_seq_length_op2, DynamicRNNLayer, \
    retrieve_seq_length_op

import numpy as np
import logging

import config


class DynamicAttentionRNNDecodeLayer(Layer):

    def __init__(
            self,
            prev_layer,
            encode_layer,
            cell_fn,  #tf.nn.rnn_cell.LSTMCell,
            cell_init_args=None,
            attention_fn=tf.contrib.seq2seq.LuongAttention,
            attention_fn_args=None,
            n_hidden=256,
            initializer=tf.random_uniform_initializer(-0.1, 0.1),
            sequence_length=None,
            initial_state=None,
            dropout=None,
            n_layer=1,
            return_seq_2d=False,
            dynamic_rnn_init_args=None,
            name='dynamic_attention_rnn_decode',
    ):
        super(DynamicAttentionRNNDecodeLayer, self).__init__(prev_layer=prev_layer, name=name)

        self.inputs = prev_layer.outputs

        if dynamic_rnn_init_args is None:
            dynamic_rnn_init_args = {}
        if cell_init_args is None:
            cell_init_args = {'state_is_tuple': True}
        if attention_fn_args is None:
            attention_fn_args = {}
        if cell_fn is None:
            raise Exception("Please put in cell_fn")
        if 'GRU' in cell_fn.__name__:
            try:
                cell_init_args.pop('state_is_tuple')
            except Exception:
                logging.warning("pop state_is_tuple fails.")

        logging.info(
            "DynamicRNNLayer %s: n_hidden:%d, in_dim:%d in_shape:%s cell_fn:%s dropout:%s n_layer:%d" % (
                self.name, n_hidden, self.inputs.get_shape().ndims, self.inputs.get_shape(), cell_fn.__name__, dropout,
                n_layer
            )
        )

        # Input dimension should be rank 3 [batch_size, n_steps(max), n_features]
        try:
            self.inputs.get_shape().with_rank(3)
        except Exception:
            raise Exception("RNN : Input dimension should be rank 3 : [batch_size, n_steps(max), n_features]")

        # Get the batch_size
        fixed_batch_size = self.inputs.get_shape().with_rank_at_least(1)[0]
        if fixed_batch_size.value:
            batch_size = fixed_batch_size.value
            logging.info("       batch_size (concurrent processes): %d" % batch_size)
        else:
            from tensorflow.python.ops import array_ops
            batch_size = array_ops.shape(self.inputs)[0]
            logging.info("       non specified batch_size, uses a tensor instead.")
        self.batch_size = batch_size

        # Creats the cell function
        # cell_instance_fn=lambda: cell_fn(num_units=n_hidden, **cell_init_args) # HanSheng

        rnn_creator = lambda: cell_fn(num_units=n_hidden, **cell_init_args)

        attention_mechanism = attention_fn(num_units=n_hidden, memory=encode_layer.outputs, memory_sequence_length=sequence_length, **attention_fn_args)

        attention_creator = lambda: tf.contrib.seq2seq.AttentionWrapper(
            rnn_creator(), attention_mechanism,
            attention_layer_size=n_hidden
        )

        # cell_instance_fn2=cell_instance_fn # HanSheng

        # Apply dropout
        if dropout:
            if isinstance(dropout, (tuple, list)):
                in_keep_prob = dropout[0]
                out_keep_prob = dropout[1]
            elif isinstance(dropout, float):
                in_keep_prob, out_keep_prob = dropout, dropout
            else:
                raise Exception("Invalid dropout type (must be a 2-D tuple of " "float)")
            try:  # TF1.0
                DropoutWrapper_fn = tf.contrib.rnn.DropoutWrapper
            except Exception:
                DropoutWrapper_fn = tf.nn.rnn_cell.DropoutWrapper

            # cell_instance_fn1=cell_instance_fn        # HanSheng
            # cell_instance_fn=DropoutWrapper_fn(
            #                     cell_instance_fn1(),
            #                     input_keep_prob=in_keep_prob,
            #                     output_keep_prob=out_keep_prob)
            cell_creator = lambda is_last=True: DropoutWrapper_fn(
                attention_creator(), input_keep_prob=in_keep_prob, output_keep_prob=out_keep_prob if is_last else 1.0
            )
        else:
            cell_creator = attention_creator

        self.cell = cell_creator()

        # Apply multiple layers
        if n_layer > 1:
            try:
                MultiRNNCell_fn = tf.contrib.rnn.MultiRNNCell
            except Exception:
                MultiRNNCell_fn = tf.nn.rnn_cell.MultiRNNCell

            if dropout:
                try:
                    # cell_instance_fn=lambda: MultiRNNCell_fn([cell_instance_fn2() for _ in range(n_layer)], state_is_tuple=True) # HanSheng
                    self.cell = MultiRNNCell_fn(
                        [cell_creator(is_last=i == n_layer - 1) for i in range(n_layer)], state_is_tuple=True
                    )
                except Exception:  # when GRU
                    # cell_instance_fn=lambda: MultiRNNCell_fn([cell_instance_fn2() for _ in range(n_layer)]) # HanSheng
                    self.cell = MultiRNNCell_fn([cell_creator(is_last=i == n_layer - 1) for i in range(n_layer)])
            else:
                try:
                    self.cell = MultiRNNCell_fn([cell_creator() for _ in range(n_layer)], state_is_tuple=True)
                except Exception:  # when GRU
                    self.cell = MultiRNNCell_fn([cell_creator() for _ in range(n_layer)])

        # self.cell=cell_instance_fn() # HanSheng

        # Initialize initial_state
        if initial_state is None:
            self.initial_state = self.cell.zero_state(batch_size, dtype=tl.layers.LayersConfig.tf_dtype)  # dtype=tf.float32)
        else:
            self.initial_state = self.cell.zero_state(batch_size, dtype=tl.layers.LayersConfig.tf_dtype).clone(cell_state=initial_state)

        # Computes sequence_length
        if sequence_length is None:
            try:  # TF1.0
                sequence_length = retrieve_seq_length_op(
                    self.inputs if isinstance(self.inputs, tf.Tensor) else tf.stack(self.inputs)
                )
            except Exception:  # TF0.12
                sequence_length = retrieve_seq_length_op(
                    self.inputs if isinstance(self.inputs, tf.Tensor) else tf.pack(self.inputs)
                )

        # Main - Computes outputs and last_states
        with tf.variable_scope(name, initializer=initializer) as vs:
            print(self.cell)
            print(self.inputs)
            print(self.initial_state)
            outputs, last_states = tf.nn.dynamic_rnn(
                cell=self.cell,
                # inputs=X
                inputs=self.inputs,
                # dtype=tf.float64,
                sequence_length=sequence_length,
                initial_state=self.initial_state,
                **dynamic_rnn_init_args
            )
            rnn_variables = tf.get_collection(tl.layers.TF_GRAPHKEYS_VARIABLES, scope=vs.name)

            # [batch_size, n_step(max), n_hidden]
            # self.outputs = result[0]["outputs"]
            # self.outputs = outputs    # it is 3d, but it is a list
            if return_seq_2d:
                # PTB tutorial:
                # 2D Tensor [n_example, n_hidden]
                try:  # TF1.0
                    self.outputs = tf.reshape(tf.concat(outputs, 1), [-1, n_hidden])
                except Exception:  # TF0.12
                    self.outputs = tf.reshape(tf.concat(1, outputs), [-1, n_hidden])
            else:
                # <akara>:
                # 3D Tensor [batch_size, n_steps(max), n_hidden]
                max_length = tf.shape(outputs)[1]
                batch_size = tf.shape(outputs)[0]

                try:  # TF1.0
                    self.outputs = tf.reshape(tf.concat(outputs, 1), [batch_size, max_length, n_hidden])
                except Exception:  # TF0.12
                    self.outputs = tf.reshape(tf.concat(1, outputs), [batch_size, max_length, n_hidden])
                    # self.outputs = tf.reshape(tf.concat(1, outputs), [-1, max_length, n_hidden])

        # Final state
        self.final_state = last_states

        self.sequence_length = sequence_length

        # self.all_layers = list(layer.all_layers)
        # self.all_params = list(layer.all_params)
        # self.all_drop = dict(layer.all_drop)

        self.all_layers.append(self.outputs)
        self.all_params.extend(rnn_variables)


class Model_KG4Text():

    def __init__(
            self,
            model_name,
            start_learning_rate,
            decay_rate,
            decay_steps,
            word_embedding_dim=config.word_embedding_dim,
            hidden_dim=config.hidden_dim,
            kg_embedding_dim=config.kg_embedding_dim,
            vocab_size=config.vocab_size
    ):
        self.model_name = model_name
        self.start_learning_rate = start_learning_rate
        self.decay_rate = decay_rate
        self.decay_steps = decay_steps
        self.word_embedding_dim = word_embedding_dim
        self.kg_embedding_dim = kg_embedding_dim
        self.hidden_dim = hidden_dim
        self.vocab_size = vocab_size

        self.__create_placeholders__()
        self.__create_model__()
        self.__create_loss__()
        self.__create_training_op__()


    def __create_placeholders__(self):
        self.encode_seqs = tf.placeholder(dtype=tf.int32, shape=[config.batch_size, None], name="encode_seqs")
        self.kg_vector = tf.placeholder(dtype=tf.float32, shape=[config.batch_size, None, self.kg_embedding_dim], name="kg_score")
        self.decode_seqs = tf.constant(np.zeros([config.batch_size, 1, self.word_embedding_dim + self.kg_embedding_dim]), dtype=tf.float32)

        self.encode_seqs_inference = tf.placeholder(dtype=tf.int32, shape=[1, None], name="encode_seqs_inference")


    def __create_model__(self):
        self.__get_network__(self.model_name, self.encode_seqs, self.kg_vector, self.decode_seqs, reuse=False, is_train=True)


    def __get_network__(self, model_name, encode_seqs, kg_vector, decode_seqs, reuse=False, is_train=True):
        with tf.variable_scope(model_name, reuse=reuse):

            net_word_embed = EmbeddingInputlayer(
                inputs=encode_seqs,
                vocabulary_size = self.vocab_size,
                embedding_size = self.word_embedding_dim,
                name='seq_embedding'
            )
            net_kg = InputLayer(
                inputs=kg_vector,
                name='in_kg'
            )
            net_in = ConcatLayer(
                [net_word_embed, net_kg],
                concat_dim=-1,
                name='concat_kg_word'
            )

            net_encoder = DynamicRNNLayer(
                net_in,
                cell_fn = tf.contrib.rnn.BasicLSTMCell,
                n_hidden = self.hidden_dim,
                # dropout = (0.7 if is_train else None),
                sequence_length = tl.layers.retrieve_seq_length_op2(encode_seqs),
                return_last = False,
                return_seq_2d = False,
                name = 'net_encoder'
            )

            net_decode_in = InputLayer(
                inputs=decode_seqs,
                name='net_decode_in'
            )

            net_decoder = DynamicAttentionRNNDecodeLayer(
                net_decode_in,
                net_encoder,
                cell_fn = tf.contrib.rnn.BasicLSTMCell,
                attention_fn=tf.contrib.seq2seq.LuongAttention,
                n_hidden = self.hidden_dim,
                # dropout = (0.7 if is_train else None),
                # initial_state=net_encoder.final_state,
                sequence_length = tl.layers.retrieve_seq_length_op2(encode_seqs),
                return_seq_2d = False,
                name = 'net_decoder_attention'
            )
            print(net_decoder.outputs)

        return



    def __create_loss__(self):
        pass

    def __create_training_op__(self):
        self.global_step = tf.placeholder(
            dtype=tf.int32,
            shape=[],
            name="global_step"
        )
        self.learning_rate = tf.train.exponential_decay(
            learning_rate=self.start_learning_rate,
            global_step=self.global_step,
            decay_steps=self.decay_steps,
            decay_rate=self.decay_rate,
            staircase=True,
            name="learning_rate"
        )
        # self.optim = tf.train.AdamOptimizer(self.learning_rate, beta1=0.5) \
        #     .minimize(self.train_loss, var_list=self.train_net.all_params)


if __name__ == "__main__":
    model = Model_KG4Text(
        model_name="text_encoding",
        start_learning_rate=0.001,
        decay_rate=0.8,
        decay_steps=1000
    )
    pass
