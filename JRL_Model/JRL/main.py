# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#		 http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""Binary for training translation models and decoding from them.

Running this program without --decode will download the WMT corpus into
the directory specified as --data_dir and tokenize it in a very basic way,
and then start training a model saving checkpoints to --train_dir.

Running with --decode starts an interactive loop so you can see how
the current checkpoint translates English sentences into French.

See the following papers for more information on neural translation models.
 * http://arxiv.org/abs/1409.3215
 * http://arxiv.org/abs/1409.0473
 * http://arxiv.org/abs/1412.2007
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from tensorflow.python.client import timeline

import math
import os
import random
import sys
import time

import numpy as np
from six.moves import xrange	# pylint: disable=redefined-builtin
import tensorflow as tf
import data_util

from MultiViewEmbedding import MultiViewEmbedding_model


tf.app.flags.DEFINE_float("learning_rate", 0.05, "Learning rate.")
tf.app.flags.DEFINE_float("learning_rate_decay_factor", 0.90,
							"Learning rate decays by this much.")
tf.app.flags.DEFINE_float("max_gradient_norm", 5.0,
							"Clip gradients to this norm.")
tf.app.flags.DEFINE_float("subsampling_rate", 1e-4,
							"The rate to subsampling.")
tf.app.flags.DEFINE_float("L2_lambda", 0.0,
							"Lambda for L2 regularization.")
tf.app.flags.DEFINE_float("image_weight", 1.0,
							"weight for image loss.")
tf.app.flags.DEFINE_integer("batch_size", 64,
							"Batch size to use during training.")
#rank list size should be read from data
tf.app.flags.DEFINE_string("data_dir", "/tmp", "Data directory")
tf.app.flags.DEFINE_string("input_train_dir", "", "The directory of training and testing data")
tf.app.flags.DEFINE_string("train_dir", "/tmp", "Model directory & output directory")
tf.app.flags.DEFINE_string("similarity_func", "product", "Select similarity function")
tf.app.flags.DEFINE_string("net_struct", "pv", "Select network structure")
tf.app.flags.DEFINE_integer("embed_size", 100, "Size of each embedding.")
tf.app.flags.DEFINE_integer("window_size", 5, "Size of context window.")
tf.app.flags.DEFINE_integer("max_train_data_size", 0,
							"Limit on the size of training data (0: no limit).")
tf.app.flags.DEFINE_integer("max_train_epoch", 5,
							"Limit on the epochs of training (0: no limit).")
tf.app.flags.DEFINE_integer("steps_per_checkpoint", 200,
							"How many training steps to do per checkpoint.")
tf.app.flags.DEFINE_integer("seconds_per_checkpoint", 3600,
							"How many seconds to store embeddings.")
tf.app.flags.DEFINE_integer("negative_sample", 5,
							"How many negative samples to generate for listMLE.")
tf.app.flags.DEFINE_boolean("decode", False,
							"Set to True for decoding data.")
tf.app.flags.DEFINE_string("test_mode", "product_scores", "The output methods")
tf.app.flags.DEFINE_integer("rank_cutoff", 100,
							"Rank cutoff for output ranklists.")
tf.app.flags.DEFINE_boolean("self_test", False,
							"Run a self-test if this is set to True.")



FLAGS = tf.app.flags.FLAGS


def create_model(session, forward_only, data_set, review_size):
	"""Create translation model and initialize or load parameters in session."""
	model = MultiViewEmbedding_model(
			data_set, data_set.vocab_size, review_size, data_set.user_size,
			data_set.product_size, data_set.vocab_distribute, data_set.review_distribute, data_set.product_distribute,
			FLAGS.window_size, FLAGS.embed_size, FLAGS.max_gradient_norm, FLAGS.batch_size,
			FLAGS.learning_rate, FLAGS.L2_lambda, FLAGS.image_weight, FLAGS.net_struct, FLAGS.similarity_func, forward_only, FLAGS.negative_sample)
	ckpt = tf.train.get_checkpoint_state(FLAGS.train_dir)
	if ckpt:
		print("Reading model parameters from %s" % ckpt.model_checkpoint_path)
		model.saver.restore(session, ckpt.model_checkpoint_path)
	else:
		print("Created model with fresh parameters.")
		session.run(tf.global_variables_initializer())
	return model


def train():
	# Prepare data.
	print("Reading data in %s" % FLAGS.data_dir)
	
	data_set = data_util.Tensorflow_data(FLAGS.data_dir, FLAGS.input_train_dir, 'train')
	data_set.sub_sampling(FLAGS.subsampling_rate)

	# add image features
	data_set.read_image_features(FLAGS.data_dir)

	# add rating features
	data_set.read_latent_factor(FLAGS.data_dir)

	config = tf.ConfigProto()
	config.gpu_options.allow_growth = True
	#config.log_device_placement=True
	with tf.Session(config=config) as sess:
		# Create model.
		print("Creating model")
		model = create_model(sess, False, data_set, data_set.review_size)

		print('Start training')
		words_to_train = float(FLAGS.max_train_epoch * data_set.word_count) + 1
		current_words = 0.0
		previous_words = 0.0
		start_time = time.time()
		last_check_point_time = time.time()
		step_time, loss = 0.0, 0.0
		current_epoch = 0
		current_step = 0
		get_batch_time = 0.0
		training_seq = [i for i in xrange(data_set.review_size)]
		model.setup_data_set(data_set, words_to_train)
		while True:
			random.shuffle(training_seq)
			model.intialize_epoch(training_seq)
			has_next = True
			while has_next:
				time_flag = time.time()
				user_idxs, product_idxs, review_idxs, word_idxs, context_word_idxs, learning_rate, has_next = model.get_train_batch()
				get_batch_time += time.time() - time_flag

				if len(word_idxs) > 0:
					time_flag = time.time()
					step_loss, _ = model.step(sess, learning_rate, user_idxs, product_idxs, 
								review_idxs, word_idxs, context_word_idxs, False)
					#step_time += (time.time() - start_time) / FLAGS.steps_per_checkpoint
					loss += step_loss / FLAGS.steps_per_checkpoint
					current_step += 1
					step_time += time.time() - time_flag

				# Once in a while, we print statistics.
				if current_step % FLAGS.steps_per_checkpoint == 0:
					print("Epoch %d Words %d/%d: lr = %5.3f loss = %6.2f words/sec = %5.2f prepare_time %.2f step_time %.2f\r" %
							(current_epoch, model.finished_word_num, model.words_to_train, learning_rate, loss, 
								(model.finished_word_num- previous_words)/(time.time() - start_time), get_batch_time, step_time), end="")
					step_time, loss = 0.0, 0.0
					current_step = 1
					get_batch_time = 0.0
					sys.stdout.flush()
					previous_words = model.finished_word_num
					start_time = time.time()
					#print('time: ' + str(time.time() - last_check_point_time))
					#if time.time() - last_check_point_time > FLAGS.seconds_per_checkpoint:
					#	checkpoint_path_best = os.path.join(FLAGS.train_dir, "MultiViewEmbedding.ckpt")
					#	model.saver.save(sess, checkpoint_path_best, global_step=model.global_step)

			current_epoch += 1
			#checkpoint_path_best = os.path.join(FLAGS.train_dir, "MultiViewEmbedding.ckpt")
			#model.saver.save(sess, checkpoint_path_best, global_step=model.global_step)
			if current_epoch >= FLAGS.max_train_epoch:	
				break
		checkpoint_path_best = os.path.join(FLAGS.train_dir, "MultiViewEmbedding.ckpt")
		model.saver.save(sess, checkpoint_path_best, global_step=model.global_step)




def get_product_scores():
	# Prepare data.
	print("Reading data in %s" % FLAGS.data_dir)
	
	data_set = data_util.Tensorflow_data(FLAGS.data_dir, FLAGS.input_train_dir, 'test')
	data_set.read_train_product_ids(FLAGS.input_train_dir)
	# add image features
	data_set.read_image_features(FLAGS.data_dir)
	# add rating features
	data_set.read_latent_factor(FLAGS.data_dir)

	current_step = 0
	config = tf.ConfigProto()
	config.gpu_options.allow_growth = True
	with tf.Session(config=config) as sess:
		# Create model.
		print("Read model")
		model = create_model(sess, True, data_set, data_set.train_review_size)
		user_ranklist_map = {}
		user_ranklist_score_map = {}
		print('Start Testing')
		words_to_train = float(FLAGS.max_train_epoch * data_set.word_count) + 1
		test_seq = [i for i in xrange(data_set.review_size)]
		model.setup_data_set(data_set, words_to_train)
		model.intialize_epoch(test_seq)
		has_next = True
		while has_next:
			user_idxs, product_idxs, review_idxs, word_idxs, context_word_idxs, learning_rate, has_next = model.get_test_batch()

			if len(user_idxs) > 0:
				user_product_scores, _ = model.step(sess, learning_rate, user_idxs, product_idxs, 
							review_idxs, word_idxs, context_word_idxs, True)
				current_step += 1

			# record the results
			for i in xrange(len(user_idxs)):
				u_idx = user_idxs[i]
				sorted_product_idxs = sorted(range(len(user_product_scores[i])), 
									key=lambda k: user_product_scores[i][k], reverse=True)
				user_ranklist_map[u_idx],user_ranklist_score_map[u_idx] = data_set.compute_test_product_ranklist(u_idx, user_product_scores[i],
													sorted_product_idxs, FLAGS.rank_cutoff) #(product name, rank)
			if current_step % FLAGS.steps_per_checkpoint == 0:
				print("Finish test review %d/%d\r" %
						(model.cur_review_i, model.review_size), end="")

	data_set.output_ranklist(user_ranklist_map, user_ranklist_score_map, FLAGS.train_dir, FLAGS.similarity_func)
	return

def output_embedding():
	# Prepare data.
	print("Reading data in %s" % FLAGS.data_dir)
	
	data_set = data_util.Tensorflow_data(FLAGS.data_dir,FLAGS.input_train_dir, 'test')
	data_set.read_train_product_ids(FLAGS.input_train_dir)
	# add image features
	data_set.read_image_features(FLAGS.data_dir)
	# add rating features
	data_set.read_latent_factor(FLAGS.data_dir)

	config = tf.ConfigProto()
	config.gpu_options.allow_growth = True
	with tf.Session(config=config) as sess:
		# Create model.
		print("Read model")
		model = create_model(sess, True, data_set, data_set.train_review_size)
		user_ranklist_map = {}
		print('Start Testing')
		words_to_train = float(FLAGS.max_train_epoch * data_set.word_count) + 1
		test_seq = [i for i in xrange(data_set.review_size)]
		model.setup_data_set(data_set, words_to_train)
		model.intialize_epoch(test_seq)
		has_next = True
		user_idxs, product_idxs, review_idxs, word_idxs, context_word_idxs, learning_rate, has_next = model.get_test_batch()

		if len(user_idxs) > 0:
			part_1 , part_2 = model.step(sess, learning_rate, user_idxs, product_idxs, 
						review_idxs, word_idxs, context_word_idxs, True, FLAGS.test_mode)

			# record the results
			user_emb = part_1[0]
			product_emb = part_1[1]
			data_set.output_embedding(user_emb, FLAGS.train_dir + 'user_emb.txt')
			data_set.output_embedding(product_emb, FLAGS.train_dir + 'product_emb.txt')
	return


#done
def self_test():
	print("Self_test")
	FLAGS.data_dir = '/mnt/scratch/aiqy/MultiViewEmbedding/working/Amazon/small_sample/min_count1/'
	# Prepare data.
	print("Reading data in %s" % FLAGS.data_dir)
	
	data_set = data_util.Tensorflow_data(FLAGS.data_dir, FLAGS.input_train_dir, 'train')
	data_set.sub_sampling(FLAGS.subsampling_rate)

	# add image features
	data_set.read_image_features(FLAGS.data_dir)

	config = tf.ConfigProto()
	config.gpu_options.allow_growth = True
	with tf.Session(config=config) as sess:
		# Create model.
		print("Creating model")
		model = create_model(sess, False, data_set, data_set.review_size)

		# This is the training loop.

		print('Start training')
		words_to_train = float(FLAGS.max_train_epoch * data_set.word_count) + 1
		current_words = 0.0
		previous_words = 0.0
		start_time = time.time()
		step_time, loss = 0.0, 0.0
		current_epoch = 0
		current_step = 0
		get_batch_time = 0.0
		training_seq = [i for i in xrange(data_set.review_size)]
		model.setup_data_set(data_set, words_to_train)
		while True:
			random.shuffle(training_seq)
			model.intialize_epoch(training_seq)
			has_next = True
			while has_next:
				time_flag = time.time()
				user_idxs, product_idxs, review_idxs, word_idxs, context_word_idxs, learning_rate, has_next = model.get_train_batch()
				get_batch_time += time.time() - time_flag

				if len(word_idxs) > 0:
					time_flag = time.time()
					step_loss, _ = model.step(sess, learning_rate, user_idxs, product_idxs, 
								review_idxs, word_idxs, context_word_idxs, False)
					#step_time += (time.time() - start_time) / FLAGS.steps_per_checkpoint
					loss += step_loss / FLAGS.steps_per_checkpoint
					current_step += 1
					step_time += time.time() - time_flag

				# Once in a while, we print statistics.
				if current_step % FLAGS.steps_per_checkpoint == 0:
					print("Epoch %d Words %d/%d: lr = %5.3f loss = %6.2f words/sec = %5.2f prepare_time %.2f step_time %.2f\r" %
							(current_epoch, model.finished_word_num, model.words_to_train, learning_rate, loss, 
								(model.finished_word_num- previous_words)/(time.time() - start_time), get_batch_time, step_time), end="")
					step_time, loss = 0.0, 0.0
					current_step = 1
					get_batch_time = 0.0
					sys.stdout.flush()
					previous_words = model.finished_word_num
					start_time = time.time()

			current_epoch += 1
			if current_epoch >= FLAGS.max_train_epoch:	
				break


def main(_):
	if FLAGS.input_train_dir == "":
		FLAGS.input_train_dir = FLAGS.data_dir

	if FLAGS.self_test:
		self_test()
	elif FLAGS.decode:
		if FLAGS.test_mode == 'output_embedding':
			output_embedding()
		else:
			get_product_scores()
	else:
		train()

if __name__ == "__main__":
	tf.app.run()
