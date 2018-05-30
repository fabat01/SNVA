import argparse
from datetime import datetime
import logging
from logging.handlers import QueueHandler
from multiprocessing import BoundedSemaphore, Process, Queue
import numpy as np
import os
import platform
import signal
from subprocess import Popen, PIPE, check_call, CalledProcessError
import sys
import tensorflow as tf
from threading import Thread
from time import time
from utils.io import IO
from utils.timestamp import Timestamp

path = os.path


# Logger thread: listens for updates to our log queue and writes them as they come in
# Terminates after we add None to the queue
def logger_thread(q):
  while True:
    record = q.get()
    if record is None:
      logging.debug('Terminating log thread')
      break
    if args.noisy:
      print(record.getMessage())
    logger = logging.getLogger(record.name)
    logger.handle(record)


def preprocess_for_inception(image):
  if image.dtype != tf.float32:
    image = tf.image.convert_image_dtype(image, dtype=tf.float32)
  if image.shape[0] != args.modelinputsize or image.shape[1] != args.modelinputsize:
    image = tf.expand_dims(image, 0)
    image = tf.image.resize_bilinear(
      image, [args.modelinputsize, args.modelinputsize], align_corners=False)
    image = tf.squeeze(image, [0])
  image = tf.subtract(image, 0.5)
  image = tf.multiply(image, 2.0)
  return image

# Mount an nfs share at a specified directory
def mount_nfs(sharepath, mountpath, username, password):
  if (not path.exists(mountpath)):
    logging.debug("Mount path {} does not exist, creating...".format(mountpath))
    os.makedirs(mountpath)
  logging.debug('Mounting NFS Share {} at directory {}'.format(sharepath, mountpath))
  mountCommand = 'sudo mount -t nfs '
  if username != None and password != None:
    logging.debug('NFS Username/password provided')
    mountCommand += '-o username=' + username + ',password=' + password + ' '
  mountCommand += sharepath + ' ' + mountpath
  try:
    check_call(mountCommand, shell=True)
  except CalledProcessError:
    logging.error("Failed to mount nfs share")
    return None
  return mountpath

# Unmount nfs share
def unmount_nfs(mountpath):
  try:
    check_call('sudo umount ' + mountpath, shell=True)
  except CalledProcessError:
    logging.error("Failed to unmount nfs share")


def analyze_video(video_file_path, video_frame_generator, video_frame_shape, batch_size,
                  session_config, input_node, output_node, prob_array, child_process_semaphore,
                  device_id_queue, device_type, device_count, process_id):
  logging.debug('Child process {} is constructing image dataset pipeline'.format(process_id))

  video_frame_dataset = tf.data.Dataset.from_generator(
    video_frame_generator, tf.uint8, tf.TensorShape(list(video_frame_shape)))

  video_frame_dataset = video_frame_dataset.map(
    preprocess_for_inception, num_parallel_calls=int(os.cpu_count() / device_count))

  video_frame_dataset = video_frame_dataset.batch(batch_size)

  video_frame_dataset = video_frame_dataset.prefetch(batch_size)

  next_batch = video_frame_dataset.make_one_shot_iterator().get_next()

  device_id = device_id_queue.get()
  logging.debug('Child process {} acquired {} device with id {}'.format(
    process_id, device_type, device_id))

  if device_type == 'gpu':
    logging.info('Child process {} is setting CUDA_VISIBLE_DEVICES environment variable to {}.'
                 .format(process_id, device_id))
    os.environ['CUDA_VISIBLE_DEVICES'] = device_id
  else:
    logging.info('Setting CUDA_VISIBLE_DEVICES environment variable to None.')
    os.environ['CUDA_VISIBLE_DEVICES'] = ''

  attempts = 0

  while attempts < 3:
    try:
      logging.debug('Child process {} is starting inference on video at path {}'.format(
        process_id, video_file_path))

      start = time()

      with tf.device('/cpu:0') if device_type == 'cpu' else tf.device(None):
        with tf.Session(config=session_config) as session:
          num_processed_frames = 0

          while True:
            try:
              video_frame_batch = session.run(next_batch)
              probs = session.run(output_node, {input_node: video_frame_batch})
              num_probs = probs.shape[0]
              prob_array[num_processed_frames:num_processed_frames + num_probs] = probs
              num_processed_frames += num_probs
            except tf.errors.OutOfRangeError:
              logging.info('Child process {} has completed inference on video at path {}'.format(
                process_id, video_file_path))
              break

      end = time() - start

      logging.debug('Child process {} released device_id {}'.format(process_id, device_id))
      device_id_queue.put(device_id)

      IO.print_processing_duration(
        end, 'Child process {} processed {} frames in'.format(process_id, num_processed_frames))

      break
    # TODO: permanently update the batch size so as to not waste time on future batches.
    # in the limit, we should only detect OOM once and update a shared batch size variable
    # to benefit all future videos within the current app run.
    except tf.errors.ResourceExhaustedError as ree:
      logging.warning('Resources reportedly exhausted.')
      logging.warning(ree)
      attempts += 1

      # If an error occurs, retry up to two times
      if attempts < 3:
        batch_size = int(batch_size / 2)
        logging.error('Child process {} will re-attempt inference with a new batch size of {}'
          .format(process_id, batch_size))
      else:
        logging.error('Child process {} will not re-attempt inference.'.format(process_id))
        logging.debug('Child process {} released device_id {}'.format(process_id, device_id))
        device_id_queue.put(device_id)

        logging.debug('Child process {} released semaphore back to parent process {}'.format(
          process_id, os.getppid()))
        child_process_semaphore.release()

        logging.debug('Child process {} terminated itself'.format(process_id))
        exit()
    except tf.errors.InternalError as ie:
      logging.error('Child process {} encountered an internal error occured while during '
                    'analysis'.format(process_id))
      logging.error(ie)

      attempts += 1

      if attempts < 3:
        logging.error('Child process {} will re-attempt inference.'.format(process_id))
      else:
        logging.error('Child process {} will not re-attempt inference.'.format(process_id))
        logging.debug('Child process {} released device_id {}'.format(process_id, device_id))
        device_id_queue.put(device_id)

        logging.debug('Child process {} released semaphore back to parent process {}'.format(
          process_id, os.getppid()))
        child_process_semaphore.release()

        logging.debug('Child process {} terminated itself'.format(process_id))
        exit()
    except Exception as e:
      logging.error('Child process {} encountered an unexpected error during analysis.'.format(
        process_id))
      logging.error(e)

      logging.debug('Child process {} released device_id {}'.format(process_id, device_id))
      device_id_queue.put(device_id)

      logging.debug('Child process {} released semaphore back to parent process {}'.format(
        process_id, os.getppid()))
      child_process_semaphore.release()

      logging.debug('Child process {} terminated itself'.format(process_id))
      exit()


def process_video(video_file_path, class_names, model_map, device_id_queue, child_process_semaphore,
                  logqueue, loglevel, device_type, device_count, ffprobe_path, ffmpeg_path):
  # Configure logging for this process
  qh = QueueHandler(logqueue)
  root = logging.getLogger()

  # Clear any handlers to avoid duplicate entries
  if root.hasHandlers():
    root.handlers.clear()
  root.setLevel(loglevel)
  root.addHandler(qh)

  def interrupt_handler(signal_number, _):
    logging.warning('Received interrupt signal (%d).', signal_number)
    try:
      logging.info('Unsetting CUDA_VISIBLE_DEVICES environment variable.')
      os.environ.pop('CUDA_VISIBLE_DEVICES')
    except KeyError as ke:
      logging.error(ke)
    logging.warning('Signaling logger to terminate.')
    logqueue.put(None)
    sys.exit(0)

  signal.signal(signal.SIGINT, interrupt_handler)

  video_file_name = path.basename(video_file_path)
  video_file_name, _ = path.splitext(video_file_name)

  process_id = os.getpid()

  logging.info('Child process {} is preparing to process {}'.format(process_id, video_file_name))

  try:
    frame_width, frame_height, num_frames = IO.get_video_dimensions(
      video_file_path, ffprobe_path)
  except Exception as e:
    logging.error('Child process {} received an error while fetching video dimensions.'.format(
      process_id))
    logging.error(e)
    logging.error('Child process {} released semaphore back to parent process {}'.format(
      process_id, os.getppid()))
    child_process_semaphore.release()
    logging.debug('Child process {} terminated itself'.format(process_id))
    exit()

  batch_size = args.batchsize

  logging.debug('Constructing ffmpeg command')
  command = [ffmpeg_path, '-i', video_file_path]

  if args.crop and all([frame_width >= args.cropwidth > 0, frame_height >= args.cropheight > 0,
                        frame_width > args.cropx >= 0, frame_height > args.cropy >= 0]):
    command.extend(['-vf', 'crop=w={}:h={}:x={}:y={}'.format(
      args.cropwidth, args.cropheight, args.cropx, args.cropy)])

    frame_width = args.cropwidth
    frame_height = args.cropheight

  command.extend(['-vcodec', 'rawvideo', '-pix_fmt', 'rgb24', '-vsync', 'vfr',
                  '-hide_banner', '-loglevel', '0', '-f', 'image2pipe', 'pipe:1'])

  # log the constructed command string if debug
  if loglevel == logging.DEBUG:
    IO.print_subprocess_command(command)

  if not args.excludetimestamps:
    timestamp_array = np.ndarray(
      (args.timestampheight * num_frames, args.timestampmaxwidth, args.numchannels), dtype='uint8')

  video_frame_shape = (frame_height, frame_width, args.numchannels)
  
  # feed the tf.data input pipeline one image at a time and, while we're at it,
  # extract timestamp overlay crops for later mapping to strings.
  def video_frame_generator():
    if not args.excludetimestamps:
      i = 0

      tx = args.timestampx - args.cropx
      ty = args.timestampy - args.cropy
      th = args.timestampheight
      tw = args.timestampmaxwidth

    num_channels = args.numchannels

    video_frame_string_len = frame_width * frame_height * num_channels

    logging.info('Child process {} is opening image pipe for {}'.format(process_id, video_file_name))

    # TODO: set buffsize equal to the smallest multiple of a power of two >= batch_size * video_frame_size_in_bytes
    with Popen(command, stdout=PIPE, bufsize=batch_size * 2 * 512 ** 2) as video_frame_pipe:
      while True:
        try:
          video_frame_string = video_frame_pipe.stdout.read(video_frame_string_len)
          if not video_frame_string:
            logging.info('Child process {} is closing image pipe'.format(process_id))
            video_frame_pipe.stdout.close()
            video_frame_pipe.terminate()
            return

          video_frame_array = np.fromstring(video_frame_string, dtype=np.uint8)
          video_frame_array = np.reshape(video_frame_array, video_frame_shape)

          if not args.excludetimestamps:
            timestamp_array[th * i:th * (i + 1)] = video_frame_array[ty:ty + th, tx:tx + tw]
            i += 1

          yield video_frame_array
        except Exception as e:
          logging.error('Child process {} met an unexpected error after processing {} frames.'
                        .format(process_id, i))
          logging.error(e)
          logging.error('Child process {} is closing image pipe'.format(process_id))
          video_frame_pipe.stdout.close()
          video_frame_pipe.terminate()
          logging.error('Child process {} is raising exception to caller.'.format(process_id))
          raise e

  # pre-allocate memory for prediction storage
  num_classes = len(class_names)
  probability_array = np.ndarray((num_frames, num_classes), dtype=np.float32)

  analyze_video(
    video_file_path, video_frame_generator, video_frame_shape, batch_size, model_map['session_config'],
    model_map['input_node'], model_map['output_node'],  probability_array, child_process_semaphore,
    device_id_queue, device_type, device_count, process_id)

  if args.excludetimestamps:
    timestamp_strings = None
  else:
    try:
      start = time()

      timestamp_object = Timestamp(args.timestampheight, args.timestampmaxwidth)
      timestamp_strings = timestamp_object.stringify_timestamps(timestamp_array)

      end = time() - start

      IO.print_processing_duration(
        end, 'Child process {} converted timestamp images to strings for {} in'.format(
          process_id, video_file_name))
    except Exception as e:
      logging.error(
        'An unexpected error occured while converting timestamp images to strings for {}'.
          format(video_file_name))
      logging.error('Throwing exception to main process')
      raise e

  try:
    start = time()

    IO.write_report(
      video_file_name, args.reportpath, args.excludetimestamps, timestamp_strings, probability_array,
      class_names, args.smoothprobs, args.smoothingfactor, args.binarizeprobs, process_id)

    end = time() - start

    IO.print_processing_duration(
      end, 'Child process {} generated report for {} in'.format(process_id, video_file_name))
  except Exception as e:
    logging.error('An unexpected error occured while generating report on {}'.
                     format(video_file_name))
    logging.error('Throwing exception to main process')
    raise e

  logging.debug('Child process {} released semaphore back to parent process {}'.
                format(process_id, os.getppid()))
  child_process_semaphore.release()

  
def main():
  start = time()

  # Create a queue to handle log requests from multiple processes
  logqueue = Queue()
  # Configure our log in the main process to write to a file
  if path.exists(args.logpath):
    if path.isfile(args.logpath):
      raise ValueError('The specified logpath {} is expected to be a directory, not a file.'.format(args.logpath))
  else:
    logging.info("Creating log directory {}".format(args.logpath))

    os.makedirs(args.logpath)

  logging.basicConfig(filename=path.join(args.logpath, datetime.now().strftime('snva_%m_%d_%Y.log')), level=loglevel,
                      format='%(processName)-10s:%(asctime)s:%(levelname)s::%(message)s')
  # Start our listener thread
  lp = Thread(target=logger_thread, args=(logqueue,))
  lp.start()
  logging.info('Entering main process')

  try:
    FFMPEG_PATH = os.environ['FFMPEG_HOME']
  except KeyError as ke:
    logging.error('Environment variable FFMPEG_HOME not set. Attempting to use default ffmpeg binary location.')
    logging.error(ke)
    if platform.system() == 'Windows':
      FFMPEG_PATH = 'ffmpeg.exe'
    else:
      FFMPEG_PATH = '/usr/local/bin/ffmpeg' if path.exists('/usr/local/bin/ffmpeg') else '/usr/bin/ffmpeg'

  logging.debug('FFMPEG Path: {}'.format(FFMPEG_PATH))

  try:
    FFPROBE_PATH = os.environ['FFPROBE_HOME']
  except KeyError as ke:
    logging.error('Environment variable FFPROBE_HOME not set. Attempting to use default ffprobe binary location.')
    logging.error(ke)
    if platform.system() == 'Windows':
      FFPROBE_PATH = 'ffprobe.exe'
    else:
      FFPROBE_PATH = '/usr/local/bin/ffprobe' if path.exists('/usr/local/bin/ffprobe') else '/usr/bin/ffprobe'

  logging.debug('FFPROBE Path: {}'.format(FFPROBE_PATH))

  if (args.nfs):
    video_dir_path = mount_nfs(args.videopath, "./videos", args.nfs_username, args.nfs_password)
    if video_dir_path == None:
      raise ValueError('Could not connect to the specified NFS share {}'.format(args.videopath))
    video_file_names = IO.read_video_file_names(video_dir_path)
  elif path.isdir(args.videopath):
    video_dir_path = args.videopath
    video_file_names = IO.read_video_file_names(video_dir_path)
  elif path.isfile(args.videopath):
    video_dir_path, video_file_name = path.split(args.videopath)
    video_file_names = [video_file_name]
  else:
    raise ValueError('The video file/folder specified at the path {} could not be found.'.format(
      args.videopath))

  # TODO modelpath should not be required to be passed at the command line
  if not path.isfile(args.modelpath):
    raise ValueError('The model specified at the path {} could not be found.'.format(
      args.modelpath))

  if args.excludepreviouslyprocessed and path.isdir(args.reportpath):
    report_file_names = os.listdir(args.reportpath)

    if len(report_file_names) > 0:
      video_ext = path.splitext(video_file_names[0])[1]
      report_ext = path.splitext(report_file_names[0])[1]
      previously_processed_video_file_names = [name.replace(report_ext, video_ext)
                                               for name in report_file_names]
      video_file_names = [name for name in video_file_names
                          if name not in previously_processed_video_file_names]

  if not path.isdir(args.reportpath):
    os.makedirs(args.reportpath)

  if args.ionodenamespath is None or not path.isfile(args.ionodenamespath):
    model_dir_path, _ = path.split(args.modelpath)
    io_node_names_path = path.join(model_dir_path, 'io_node_names.txt')
  else:
    io_node_names_path = args.ionodenamespath
  logging.debug('io tensors path set to: {}'.format(io_node_names_path))

  if args.classnamespath is None or not path.isfile(args.classnamespath):
    model_dir_path, _ = path.split(args.modelpath)
    class_names_path = path.join(model_dir_path, 'class_names.txt')
  else:
    class_names_path = args.classnamespath
  logging.debug('labels path set to: {}'.format(class_names_path))

  if args.cpuonly:
    device_id_list = ['0']
    device_type = 'cpu'
  else:
    device_id_list = IO.get_device_ids()
    device_type = 'gpu'

  device_id_list_len = len(device_id_list)

  logging.info('Found {} available {} device(s).'.format(device_id_list_len, device_type))

  # child processes will dequeue and enqueue device names
  device_id_queue = Queue(device_id_list_len)

  for device_id in device_id_list:
    device_id_queue.put(device_id)

  label_map = IO.read_class_names(class_names_path)
  class_name_list = list(label_map.values())

  model_map = IO.load_model(args.modelpath, io_node_names_path, device_type, args.gpumemoryfraction)

  # The chief worker will allow at most device_count + 1 child processes to be created
  # since the greatest number of concurrent operations is the number of compute devices
  # plus one for IO
  child_process_semaphore = BoundedSemaphore(device_id_list_len + 1)
  child_process_list = []
  logging.info('Processing {} videos in directory: {}'.format(len(video_file_names),
                                                              video_dir_path))

  unprocessed_video_file_names = []

  def call_process_video(video_file_name, child_process_semaphore):
    # Before popping the next video off of the list and creating a process to scan it,
    # check to see if fewer than device_id_list_len + 1 processes are active. If not,
    # Wait for a child process to release its semaphore acquisition. If so, acquire the
    # semaphore, pop the next video name, create the next child process, and pass the
    # semaphore to it
    video_file_path = path.join(video_dir_path, video_file_name)

    if device_id_list_len > 1:
      logging.debug('Creating new child process.')

      child_process = Process(target=process_video,
                              name='ChildProcess:{}'.format(video_file_name),
                              args=(video_file_path, class_name_list,
                                    model_map, device_id_queue, child_process_semaphore, logqueue,
                                    loglevel, device_type, device_id_list_len, FFPROBE_PATH, FFMPEG_PATH))

      logging.debug('Starting starting child process.')

      child_process.start()

      child_process_list.append(child_process)
    else:
      logging.info('Invoking process_video() in main process because device_type == {}'.format(device_type))
      process_video(video_file_path, class_name_list, model_map, device_id_queue, child_process_semaphore, 
                     logqueue, loglevel, device_type, device_id_list_len, FFPROBE_PATH, FFMPEG_PATH)

  while len(video_file_names) > 0:
    child_process_semaphore.acquire()  # block if three child processes are active
    logging.debug('Main process {} acquired child_process_semaphore'.format(os.getpid()))

    video_file_name = video_file_names.pop()

    try:
      call_process_video(video_file_name, child_process_semaphore)
    except Exception as e:
      logging.error('An unknown error has occured. Appending {} to the end of '
                       'video_file_names to re-attemt processing later'.format(video_file_name))
      logging.error(e)
      unprocessed_video_file_names.append(video_file_name)

      logging.error('Releasing child_process_semaphore for child process with raised exception.')
      child_process_semaphore.release()

  logging.info('Joining remaining active child processes.')

  for child_process in child_process_list:
    if child_process.is_alive():
      logging.debug('Joining child process {}'.format(child_process.pid))

      child_process.join()

  if len(unprocessed_video_file_names) > 0:
    logging.info('Re-attempting to process any video that did not succeed on the first attempt')

    child_process_list.clear()

    while len(unprocessed_video_file_names) > 0:
      child_process_semaphore.acquire()  # block if three child processes are active
      logging.debug('Main process {} acquired child_process_semaphore'.format(os.getpid()))

      video_file_name = unprocessed_video_file_names.pop()
      try:
        call_process_video(video_file_name, child_process_semaphore)
      except Exception as e:
        logging.error('An unknown error has occured during the second attempt to process {}. No further attempts '
                         'will be made'.format(video_file_name))
        logging.error(e)
        logging.error('Releasing child_process_semaphore for child process with raised exception.')
        child_process_semaphore.release()

    logging.info('Joining remaining active child processes.')

    for child_process in child_process_list:
      if child_process.is_alive():
        logging.debug('Joining child process {}'.format(child_process.pid))
        child_process.join()

  end = time() - start

  IO.print_processing_duration(end, 'Video processing completed with total elapsed time: ')
  if (args.nfs):
    unmount_nfs(video_dir_path)

  # Signal the logging thread to finish up
  logging.debug('Signaling log queue to end service.')
  logqueue.put(None)
  lp.join()


if __name__ == '__main__':
  parser = argparse.ArgumentParser(description='SHRP2 NDS Video Analytics built on TensorFlow')

  parser.add_argument('--batchsize', '-bs', type=int, default=32,
                      help='The number of images fed into the neural net at a time')
  parser.add_argument('--binarizeprobs', '-b', action='store_true',
                      help='Round probabilities to zero or one. For distributions'
                           'with two 0.5 values, both will be rounded up to 1.0')
  parser.add_argument('--cpuonly', '-cpu', action='store_true', help='')
  parser.add_argument('--crop', '-c', action='store_true',
                      help='Crop video frames to [offsetheight, offsetwidth, targetheight, targetwidth]')
  parser.add_argument('--cropheight', '-ch', type=int, default=356, help='y-component of bottom-right corner of crop.')
  parser.add_argument('--cropwidth', '-cw', type=int, default=474, help='x-component of bottom-right corner of crop.')
  parser.add_argument('--cropx', '-cx', type=int, default=2, help='x-component of top-left corner of crop.')
  parser.add_argument('--cropy', '-cy', type=int, default=0, help='y-component of top-left corner of crop.')
  parser.add_argument('--excludepreviouslyprocessed', '-epp', action='store_true',
                      help='Skip processing of videos for which reports already exist in reportpath.')
  parser.add_argument('--excludetimestamps', '-et', action='store_true',
                      help='Read timestamps off of video frames and include them as strings in the output CSV.')
  parser.add_argument('--gpumemoryfraction', '-gmf', type=float, default=0.9,
                      help='% of GPU memory available to this process.')
  parser.add_argument('--ionodenamespath', '-itp', default=None, help='Path to the io tensor names text file.')
  parser.add_argument('--classnamespath', '-cnp', default=None, help='Path to the class ids/names text file.')
  parser.add_argument('--modelinputsize', '-mis', type=int, required=True,
                      help='The square input dimensions of the neural net.')
  parser.add_argument('--logpath', '-l', default='./logs', help='Path to the directory where log files are stored.')
  parser.add_argument('--numchannels', '-nc', type=int, default=3, help='The fourth dimension of image batches.')
  parser.add_argument('--modelpath', '-mp', required=True, help='Path to the model protobuf file.')
  parser.add_argument('--reportpath', '-rp', default='./results',
                      help='Path to the directory where results are stored.')
  parser.add_argument('--smoothprobs', '-sm', action='store_true',
                      help='Apply class-wise smoothing across video frame class probability distributions.')
  parser.add_argument('--smoothingfactor', '-sf', type=int, default=16,
                      help='The class-wise probability smoothing factor.')
  parser.add_argument('--timestampheight', '-th', type=int, default=16,
                      help='The length of the y-dimension of the timestamp overlay.')
  parser.add_argument('--timestampmaxwidth', '-tw', type=int, default=160,
                      help='The length of the x-dimension of the timestamp overlay.')
  parser.add_argument('--timestampx', '-tx', type=int, default=25,
                      help='x-component of top-left corner of timestamp (before cropping).')
  parser.add_argument('--timestampy', '-ty', type=int, default=340,
                      help='y-component of top-left corner of timestamp (before cropping).')
  parser.add_argument('--videopath', '-v', required=True, help='Path to video file(s).')
  parser.add_argument('--verbose', '-vb', action='store_true', help='Print additional information in logs')
  parser.add_argument('--debug', '-d', action='store_true', help='Print debug information in logs')
  parser.add_argument('--noisy', '-n', action='store_true', help='Print logs to console as well as logfile')
  parser.add_argument('--nfs', '-nfs', action='store_true', help='Indicates videopath is an nfs share')
  parser.add_argument('--nfs_username', '-nu', default=None, help='Username for videopath nfs share')
  parser.add_argument('--nfs_password', '-np', default=None, help='Password for videopath nfs share')

  args = parser.parse_args()

  os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

  # Should this be set to match our command line arg, or should we always output this level of detail?
  tf.logging.set_verbosity(tf.logging.INFO)

  # Define our log level based on arguments
  loglevel = logging.WARNING
  if args.verbose:
    loglevel = logging.INFO
  if args.debug:
    loglevel = logging.DEBUG

  main()