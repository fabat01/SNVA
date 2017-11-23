import tensorflow as tf
import sys
import os
import os.path as path
import stat
import subprocess
import timeit
import platform
import argparse
from glob import iglob
from shutil import copy

# TODO: Modify detection to only process data after inference has completed.
# TODO: Modify model unpersist function to use loaded model name vs. static assignment.
# TODO: Add support for loading multiple primary and secondary models.

parser = argparse.ArgumentParser(description='Process some video files using Machine Learning!')
parser.add_argument('--imagepath', '-tp', dest='imagepath', action='store', default='../vidtemp/',
                    help='Path to the directory where temporary files are stored.')
parser.add_argument('--fps', '-fps', dest='fps', action='store', default='1',
                    help='Frames Per Second used to sample input video. '
                         'The higher this number the slower analysis will go. Default is 1 FPS')
parser.add_argument('--videopath', '-v', dest='video_path', action='store', help='Path to video file(s).')
parser.add_argument('--allfiles', '-a', dest='allfiles', action='store_true',
                    help='Process all video files in the directory path.')

args = parser.parse_args()
currentSrcVideo = ''

if platform.system() == 'Windows':
    # path to ffmpeg bin
    FFMPEG_PATH = 'ffmpeg.exe'
else:
    # path to ffmpeg bin
    default_ffmpeg_path = '/usr/local/bin/ffmpeg'
    FFMPEG_PATH = default_ffmpeg_path if path.exists(default_ffmpeg_path) else '/usr/bin/ffmpeg'

# setup video temp directory for video frames
if not os.path.isdir(args.imagepath):
    os.mkdir(args.imagepath)


def copy_files(src_glob, dst_folder):
    for fname in iglob(src_glob):
        newfilename = os.path.basename(fname)
        copy(fname, os.path.join(dst_folder, newfilename))


def save_training_frames(framenumber):
    # copies frames/images to the passed directory for the purposes of retraining the model
    srcpath = os.path.join(args.imagepath, '')
    dstpath = os.path.join(args.trainingpath, '')
    copy_files(srcpath + '*' + str(framenumber) + '.jpg', dstpath)


def decode_video(video_path):
    video_filename, video_file_extension = path.splitext(path.basename(video_path))
    print(' ')
    print('Decoding video file ' + video_filename)
    image_dir = os.path.join(args.imagepath, str(video_filename))
    if not path.isdir(image_dir):
        os.mkdir(image_dir)
    image_path = os.path.join(image_dir, str(video_filename) + '_%04d.jpg')
    command = [
        FFMPEG_PATH, '-i', video_path,
        '-vf', 'fps=' + args.fps, '-q:v', '1', '-vsync', 'vfr', image_path, '-hide_banner', '-loglevel', '0',
    ]
    subprocess.call(command)


def load_video_filenames(relevant_path):
    included_extenstions = ['avi', 'mp4', 'asf', 'mkv']
    return [fn for fn in os.listdir(relevant_path)
            if any(fn.lower().endswith(ext) for ext in included_extenstions)]


# set start time
start = timeit.default_timer()

if args.allfiles:
    video_files = load_video_filenames(args.video_path)
    for video_file in video_files:
        video_path = os.path.join(args.video_path, video_file)
        decode_video(video_path)
else:
    decode_video(args.video_path)

print(' ')
stop = timeit.default_timer()
total_time = stop - start
mins, secs = divmod(total_time, 60)
hours, mins = divmod(mins, 60)
sys.stdout.write("Total running time: %d:%d:%d.\n" % (hours, mins, secs))