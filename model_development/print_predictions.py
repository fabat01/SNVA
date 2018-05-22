import os
import sys

import tensorflow as tf
from tensorflow.contrib import slim

from nets import inception
from preprocessing import inception_preprocessing

tf.app.flags.DEFINE_string(
    'checkpoint_path', None, 'An absolute path to a protobuf model file.')

tf.app.flags.DEFINE_string(
    'image_dir', None, 'An absolute path to a protobuf model file.')

tf.app.flags.DEFINE_integer(
    'num_classes', 4, 'The number of images analyzed concurrently.')

tf.app.flags.DEFINE_integer(
    'batch_size', 32, 'The number of images analyzed concurrently.')

tf.app.flags.DEFINE_float(
    'gpu_memory_fraction', 0.9,
    'The ratio of total memory across all available GPUs to use with this process. '
    'Defaults to a suggested max of 0.9.')

tf.app.flags.DEFINE_integer(
    'gpu_device_num', 0,
    'The device number of a single GPU to use for evaluation on a multi-GPU system. '
    'Defaults to zero.')

tf.app.flags.DEFINE_boolean(
    'cpu_only', False,
    'Explicitly assign all evaluation ops to the CPU on a GPU-enabled system. '
    'Defaults to False.')

FLAGS = tf.app.flags.FLAGS

tf.logging.set_verbosity(tf.logging.INFO)

path = os.path

# checkpoint_path = sys.argv[1]
# image_dir = sys.argv[2]
# gpu_memory_fraction = float(sys.argv[3])
# num_classes = int(sys.argv[4])

if tf.gfile.IsDirectory(FLAGS.checkpoint_path):
  checkpoint_path = tf.train.latest_checkpoint(FLAGS.checkpoint_path)

image_names = sorted(os.listdir(image_dir))
image_paths = [path.join(image_dir, image_name) for image_name in image_names]

image_size = inception.inception_v3.default_image_size

with tf.Graph().as_default():
  # Inject placeholder into the graph
  input_image_t = tf.placeholder(tf.string, name='input_image')
  image = tf.image.decode_jpeg(input_image_t, channels=3)

  # Resize the input image, preserving the aspect ratio
  # and make a central crop of the resulted image.
  # The crop will be of the size of the default image size of
  # the network.
  # I use the "preprocess_for_eval()" method instead of "inception_preprocessing()"
  # because the latter crops all images to the center by 85% at
  # prediction time (training=False).
  processed_image = inception_preprocessing.preprocess_for_eval(image,
                                                                image_size,
                                                                image_size, central_fraction=None)

  # Networks accept images in batches.
  # The first dimension usually represents the batch size.
  # In our case the batch size is one.
  processed_images = tf.expand_dims(processed_image, 0)

  # Load the inception network structure
  with slim.arg_scope(inception.inception_v3_arg_scope()):
    logits, _ = inception.inception_v3(processed_images,
                                       num_classes=num_classes,
                                       is_training=False)
  # Apply softmax function to the logits (output of the last layer of the network)
  probabilities = tf.nn.softmax(logits)

  # Get the function that initializes the network structure (its variables) with
  # the trained values contained in the checkpoint
  init_fn = slim.assign_from_checkpoint_fn(checkpoint_path, slim.get_model_variables())

  gpu_options = tf.GPUOptions(allow_growth=True,
                              per_process_gpu_memory_fraction=gpu_memory_fraction)
  session_config = tf.ConfigProto(allow_soft_placement=True, gpu_options=gpu_options)

  with tf.Session(config=session_config) as sess:
    init_fn(sess)

    for image_path in image_paths:
      image_string = tf.gfile.FastGFile(image_path, 'rb').read()
      predictions = sess.run(probabilities, {input_image_t: image_string})

      print('nwz:{:02f}, rs:{:02f}, ws:{:02f}, wz:{:02f} | {}'.format(
        predictions[0][0], predictions[0][1], predictions[0][2],
        predictions[0][3], path.basename(image_path)))
