#!/usr/bin/env python
"""Train model to predict lightning using a simple convnet.

Copyright Google Inc.
2018 Licensed under the Apache License, Version 2.0 (the "License"); you may
not use this file except in compliance with the License. You may obtain a copy
of the License at http://www.apache.org/licenses/LICENSE-2.0 Unless required
by applicable law or agreed to in writing, software distributed under the
License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS
OF ANY KIND, either express or implied. See the License for the specific
language governing permissions and limitations under the License.
"""
from __future__ import division
from __future__ import print_function
import argparse
import functools
import logging
import os
import time
import tensorflow as tf
from tensorflow import keras

def PATCH_SIZE(params):
  return (2 * params['train_patch_radius']) + 1

def reshape_into_image(features, params):
  """reshape features dict containing ref, ltg channels into image.

  Args:
    features (dict): Looks for ref, ltg entries in dict
    params (dict): command-line parameters

  Returns:
    reshaped tensor with shape [2*train_patch_radius, 2*train_patch_radius, 2]
  """
  # stack the inputs to form a 2-channel input
  # features['ref'] is [-1, height*width]
  # stacked image is [-1, height*width, n_channels]
  n_channels = 2
  print('shape of ref feature {}'.format(features['ref'].shape))
  stacked = tf.concat([features['ref'], features['ltg']], axis=1)
  height = width = PATCH_SIZE(params)
  print('shape of all features {}, will be reshaped to [{},{},{}]'.format(
      stacked.shape, height, width, n_channels))
  return tf.reshape(stacked, [height, width, n_channels])


def make_preprocess_fn(params):
  """Make preprocessing function.

  Args:
    params (dict): command-line parameters

  Returns:
    function that takes tfexample and returns img, label
  """
  def _sparse_to_dense(data, arrlen):
    return tf.expand_dims(
        tf.reshape(tf.sparse_tensor_to_dense(data, default_value=0), [arrlen]),
        -1)

  def read_and_preprocess(example_data):
    """parses tfrecord and returns image, label.

    Args:
      example_data (str): tfrecord
    Returns:
      img, label
    """
    height = width = PATCH_SIZE(params)
    parsed = tf.parse_single_example(
        example_data, {
            'ref': tf.VarLenFeature(tf.float32),
            'ltg': tf.VarLenFeature(tf.float32),
            'has_ltg': tf.FixedLenFeature([], tf.int64, 1),
        })
    parsed['ref'] = _sparse_to_dense(parsed['ref'], height * width)
    parsed['ltg'] = _sparse_to_dense(parsed['ltg'], height * width)

    # keras wants labels to be float32
    label = tf.cast(
      tf.reshape(parsed['has_ltg'], shape=[]),
      dtype=tf.float32)
    print('shape of label {}'.format(label.shape))

    img = reshape_into_image(parsed, params)
    return img, label

  return read_and_preprocess


def engineered_features(img, halfsize):
  with tf.control_dependencies([
      tf.Assert(tf.is_numeric_tensor(img), [img])
    ]):
    qtrsize = halfsize // 2
    ref_smbox = img[:, qtrsize:(qtrsize+halfsize+1), qtrsize:(qtrsize+halfsize+1), 0:1]
    ltg_smbox = img[:, qtrsize:(qtrsize+halfsize+1), qtrsize:(qtrsize+halfsize+1), 1:2]
    ref_bigbox = img[:, :, :, 0:1]
    ltg_bigbox = img[:, :, :, 1:2]
    engfeat = tf.concat([
      tf.reduce_max(ref_bigbox, [1, 2]), # [?, 64, 64, 1] -> [?, 1]
      tf.reduce_max(ref_smbox, [1, 2]),
      tf.reduce_mean(ref_bigbox, [1, 2]),
      tf.reduce_mean(ref_smbox, [1, 2]),
      tf.reduce_mean(ltg_bigbox, [1, 2]),
      tf.reduce_mean(ltg_smbox, [1, 2])
    ], axis=1)
    return engfeat


def create_combined_model(params):
  ksize = params.get('ksize', 5)
  nfil = params.get('nfil', 10)
  nlayers = params.get('nlayers', 3)
  dprob = params.get('dprob', 0.05 if params['batch_norm'] else 0.25)

  # input is a 2-channel image
  height = width = PATCH_SIZE(params)
  img = keras.Input(shape=[height, width, 2])

  # convolutional part of model
  cnn = keras.layers.BatchNormalization()(img)
  for layer in range(nlayers):
    nfilters = nfil * (layer + 1)
    cnn = keras.layers.Conv2D(nfilters, (ksize, ksize), padding='same')(cnn)
    cnn = keras.layers.Activation('elu')(cnn)
    cnn = keras.layers.BatchNormalization()(cnn)
    cnn = keras.layers.MaxPooling2D(pool_size=(2, 2))(cnn)
  cnn = keras.layers.Flatten()(cnn)
  cnn = keras.layers.Dropout(dprob)(cnn)
  cnn = keras.layers.Dense(10, activation='relu')(cnn)

  # feature engineering part of model
  engfeat = keras.layers.Lambda(
    lambda x: engineered_features(x, height//2))(img)

  # concatenate the two parts
  both = keras.layers.concatenate([cnn, engfeat])
  ltgprob = keras.layers.Dense(1, activation='sigmoid')(both)

  # create a model
  model = keras.Model(img, ltgprob)
  def rmse(y_true, y_pred):
    import tensorflow.keras.backend as K
    return K.sqrt(K.mean(K.square(y_pred - y_true), axis=-1))
  optimizer = tf.keras.optimizers.Adam(lr=params['learning_rate'],
                                       clipnorm=1.)
  model.compile(optimizer=optimizer,
                loss='binary_crossentropy',
                metrics=['accuracy', 'mse', rmse])
  return model


def print_layer(layer, message, first_n=3, summarize=1024):
  return keras.layers.Lambda((
    lambda x: tf.Print(x, [x],
                      message=message,
                      first_n=first_n,
                      summarize=summarize)))(layer)

def create_feateng_model(params):
  # input is a 2-channel image
  height = width = PATCH_SIZE(params)
  img = keras.Input(shape=[height, width, 2])

  engfeat = keras.layers.Lambda(
    lambda x: engineered_features(x, height//2))(img)
  engfeat = print_layer(engfeat, "engfeat=")

  ltgprob = keras.layers.Dense(1, activation='sigmoid')(engfeat)

  # print
  ltgprob = print_layer(ltgprob, "ltgprob=")

  # create a model
  model = keras.Model(img, ltgprob)
  def rmse(y_true, y_pred):
    import tensorflow.keras.backend as K
    return K.sqrt(K.mean(K.square(y_pred - y_true), axis=-1))
  optimizer = tf.keras.optimizers.Adam(lr=params['learning_rate'],
                                       clipnorm=1.)
  model.compile(optimizer=optimizer,
                loss='binary_crossentropy',
                metrics=['accuracy', 'mse', rmse])
  return model


def make_dataset(pattern, mode, batch_size, params):
  """Make training/evaluation dataset.

  Args:
    pattern (str): filename pattern
    mode (int): TRAIN/EVAL/PREDICT
    default_batch_size (int): batch_size
    params (dict): transpose, num_cores

  Returns:
    tf.data dataset
  """
  def _set_shapes(batch_size, images, labels):
    """Statically set the batch_size dimension."""
    if params['transpose']:
      images.set_shape(images.get_shape().merge_with(
          tf.TensorShape([None, None, None, batch_size])))
      labels.set_shape(labels.get_shape().merge_with(
          tf.TensorShape([batch_size])))
    else:
      images.set_shape(images.get_shape().merge_with(
          tf.TensorShape([batch_size, None, None, None])))
      labels.set_shape(labels.get_shape().merge_with(
          tf.TensorShape([batch_size])))

    # keras wants labels to be same shape as logits
    labels = tf.expand_dims(labels, -1)
    return images, labels


  is_training = (mode == tf.estimator.ModeKeys.TRAIN)

  # read the dataset
  dataset = tf.data.Dataset.list_files(pattern, shuffle=is_training)

  def fetch_dataset(filename):
    buffer_size = 8 * 1024 * 1024  # 8 MiB per file
    dataset = tf.data.TFRecordDataset(filename, buffer_size=buffer_size)
    return dataset

  dataset = dataset.apply(
    tf.contrib.data.parallel_interleave(
      fetch_dataset, cycle_length=64, sloppy=True))
  dataset = dataset.shuffle(batch_size * 50) # shuffle by a bit

  # convert features into images
  preprocess_fn = make_preprocess_fn(params)
  dataset = dataset.apply(
    tf.contrib.data.map_and_batch(
      preprocess_fn,
      batch_size=batch_size,
      num_parallel_batches=params['num_cores'],
      drop_remainder=True))

  if params['transpose']:
    dataset = dataset.map(
      lambda images, labels: (tf.transpose(images, [1, 2, 3, 0]), labels),
      num_parallel_calls=params['num_cores'])

  # assign static shape
  dataset = dataset.map(functools.partial(_set_shapes, batch_size))

  # prefetch data while training
  dataset = dataset.repeat()
  dataset = dataset.prefetch(tf.contrib.data.AUTOTUNE)
  return dataset


def train_and_evaluate(hparams):
  """Main train and evaluate loop.

  Args:
    hparams (dict): Command-line parameters passed in
  """
  output_dir = hparams['job_dir']
  max_steps = hparams['train_steps']

  # avoid overly frequent evaluation
  steps_per_epoch = min(1000, max_steps//10)
  num_epochs = max_steps // steps_per_epoch

  # eval batch size has to be divisible by num_cores
  eval_batch_size = min(hparams['num_eval_records'],
                        hparams['train_batch_size'])
  eval_batch_size = eval_batch_size - eval_batch_size % hparams['num_cores']
  eval_steps = hparams['num_eval_records'] // eval_batch_size
  tf.logging.info('train_batch_size=%d  eval_batch_size=%d'
                  ' train_steps=%d (%d x %d) eval_steps=%d',
                  hparams['train_batch_size'], eval_batch_size,
                  max_steps, steps_per_epoch, num_epochs,
                  eval_steps)

  # create model
  model = create_combined_model(hparams)
  #model = create_feateng_model(hparams)

  # resolve TPU and rewrite model for TPU if necessary
  if hparams['use_tpu']:
    tpu_cluster_resolver = tf.contrib.cluster_resolver.TPUClusterResolver(
        hparams['tpu'], zone=hparams['tpu_zone'], project=hparams['project'])
    model = tf.contrib.tpu.keras_to_tpu_model(
      model,
      strategy=tf.contrib.tpu.TPUDistributionStrategy(
        tpu_cluster_resolver
      )
    )

  # set up training and evaluation in a loop
  train_data = make_dataset(hparams['train_data_path'],
                            tf.estimator.ModeKeys.TRAIN,
                            hparams['train_batch_size'],
                            hparams)
  eval_data  = make_dataset(hparams['eval_data_path'],
                            tf.estimator.ModeKeys.EVAL,
                            eval_batch_size,
                            hparams)

  # train and evaluate
  start_timestamp = time.time()
  model.fit(
    train_data,
    steps_per_epoch=steps_per_epoch,
    epochs=num_epochs,
    validation_data=eval_data,
    validation_steps=eval_steps,
    verbose=2 # 1=progress 2=one line per epoch
  )
  elapsed_time = int(time.time() - start_timestamp)
  tf.logging.info('Finished training up to step %d. Elapsed seconds %d.',
                  max_steps, elapsed_time)
  tf.logging.info(model.summary())

  # export similar to Cloud ML Engine convention
  tf.logging.info('Starting to export model.')
  signature = tf.saved_model.signature_def_utils.predict_signature_def(
    inputs={'image': model.input}, outputs={'scores': model.output})
  builder = tf.saved_model.builder.SavedModelBuilder(
    os.path.join(output_dir, 'export/exporter'))
  builder.add_meta_graph_and_variables(
    sess=keras.backend.get_session(),
    tags=[tf.saved_model.tag_constants.SERVING],
    signature_def_map={
      tf.saved_model.signature_constants.DEFAULT_SERVING_SIGNATURE_DEF_KEY:
        signature
    })
  builder.save()
  tf.logging.info('Model exported successfully')


if __name__ == '__main__':
  parser = argparse.ArgumentParser(
      description='Train cnn model for lightning prediction')
  parser.add_argument(
      '--job-dir', required=True, help='output dir. could be local or on GCS')
  parser.add_argument(
      '--train_data_path',
      required=True,
      help='Pattern for training data tfrecord files. could be local or on GCS')
  parser.add_argument(
      '--eval_data_path',
      required=True,
      help='Pattern for evaluation data tfrecord files.'
      'could be local or on GCS'
  )
  parser.add_argument(
      '--train_patch_radius',
      type=int,
      default=32,
      help='predict lightning based a 2Nx2N grid; has to match preprocessing')
  parser.add_argument(
      '--train_batch_size',
      help='Batch size for training steps',
      type=int,
      default=256)
  parser.add_argument(
      '--learning_rate',
      help='Initial learning rate for training',
      type=float,
      default=0.001)
  parser.add_argument(
      '--train_steps',
      help="""\
        Steps to run the training job for. A step is one batch-size,\
        """,
      type=int,
      default=100)
  parser.add_argument(
      '--num_eval_records',
      help='Number of validation records, '
      ' has to be less than available number and'
      ' divisible by number of cores.'
      ' You can find available number from Dataflow'
      ' pipeline that created the tfrecords dataset'
      ' See: https://console.cloud.google.com/dataflow',
      type=int,
      default=128)

  # for Cloud TPU
  parser.add_argument(
      '--use_tpu',
      help=
      ('If specified, use TPU to execute the model for training and evaluation.'
       ' Else use whatever devices are available to'
       ' TensorFlow by default (e.g. CPU and GPU)'),
      dest='use_tpu',
      action='store_true')
  parser.add_argument(
      '--transpose',
      help=('If specified, makes the batch-size the last dimension.'
            ' This is more efficient on a TPU'),
      dest='transpose',
      action='store_true')
  parser.add_argument(
      '--tpu',
      default=None,
      help='The Cloud TPU to use for training. This should be either the name '
      'used when creating the Cloud TPU, or grpc://ip.address.of.tpu:8470 url.'
  )
  parser.add_argument(
      '--project',
      default=None,
      help='Project name for the Cloud TPU-enabled project. If not specified, '
      'will attempt to automatically detect the GCE project from metadata.')
  parser.add_argument(
      '--tpu_zone',
      default=None,
      help='GCE zone where the Cloud TPU is located in. If not specified, we '
      'will attempt to automatically detect the GCE project from metadata.')
  parser.add_argument(
      '--num_cores', default=8, type=int, help='Number of TPU cores to use')

  # optional hyperparameters used by cnn
  parser.add_argument(
      '--ksize', help='kernel size of each layer for CNN', type=int, default=5)
  parser.add_argument(
      '--nfil',
      help='number of filters in each layer for CNN',
      type=int,
      default=10)
  parser.add_argument(
      '--nlayers', help='number of layers in CNN (<= 5)', type=int, default=3)
  parser.add_argument(
      '--dprob', help='dropout probability for CNN', type=float, default=0.25)
  parser.add_argument(
      '--batch_norm',
      help='if specified, do batch_norm for CNN',
      dest='batch_norm',
      action='store_true')

  logging.basicConfig(level=getattr(logging, 'INFO', None))
  parser.set_defaults(use_tpu=False, batch_norm=False)
  options = parser.parse_args().__dict__

  # run the training job
  train_and_evaluate(options)
