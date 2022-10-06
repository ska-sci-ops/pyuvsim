# -*- mode: python; coding: utf-8 -*
# Copyright (c) 2018 Radio Astronomy Software Group
# Licensed under the 3-clause BSD License
"""
Generate a test pattern based on ascii characters.

Place test pattern above given location on a given date.
"""
import argparse
import sys

import matplotlib.image as mpimg
import numpy as np
import psutil
import pylab as pl
from astropy import units as u
from astropy.coordinates import Angle, EarthLocation, SkyCoord
from astropy.time import Time


def removeallspaces(s):
    """Remove all spaces."""
    return ''.join([c for c in s if c != ' '])


if sys.version_info >= (3, 0):
    xrange = range

parser = argparse.ArgumentParser(
    description=("make a test pattern catalog that spells out something"))

parser.add_argument('-t', '--text', dest='text', default='EH',
                    type=str, help='Text to make into sources')
parser.add_argument('-s', dest='spacing', type=float, default=1.,
                    help='source spacing in degrees')
parser.add_argument('-n', dest='char_pix_height', type=int, default=10,
                    help='how many sources high should a letter be')
parser.add_argument('--jd', metavar='jd', type=float, default=2468000)
parser.add_argument('--thresh', metavar='thresh', type=float, default=140,
                    help="""Threshold for converting font pixels to sources.
                    number between 1 and 255.""")
parser.add_argument('--array_location', type=str, default='-30,116',
                    help='LAT,LON in degrees')
parser.add_argument('--plot', action='store_true')
args = parser.parse_args()

catname = removeallspaces(args.text)
imgfname = catname + '.bmp'

# first lets construct our image
fontsize = 10  # this ends up being arbitrary here
emsize = 1 / 72. * fontsize  # height of a letter in inches
dpi = np.int(np.ceil(args.char_pix_height / emsize))
# dpi = height of a letter in pixels/hight of letter in inches
text = args.text.upper()
im_cmd = ["convert",
          "-background", "black",
          "-fill", "white",
          "-font", "Keyboard",  # still looking for optimal font
          "-pointsize", "10",
          "-units", "PixelsPerInch",
          "-density", str(dpi),
          "-trim", "+repage",
          "-antialias",
          "label:" + text,
          "{imfilename}".format(imfilename=imgfname)]
print(im_cmd)
print("generating image file", imgfname)
p = psutil.Popen(im_cmd)
print(p.communicate())
p.wait(timeout=2)

jd = args.jd
print("processing image into a catalog")
im = mpimg.imread(imgfname)
arr = im[:, :, 0]  # image is rgb but actually white, grab the r channel
arr = arr[::-1, :]  # flip updown
print(arr)
# get the coordinates for each pixel
nx, ny = arr.shape
y, x = np.where(arr > args.thresh)
dx = x - int(np.floor(nx / 2.))
dy = y - int(np.floor(ny / 2.))

pixsize_deg = 1.0

zas = np.sqrt(dx ** 2 + dy ** 2) * pixsize_deg
aztmp = np.arctan2(dy, dx) * 180. / np.pi
azs = np.arctan2(dy, dx) * 180. / np.pi - 90.

alts = 90. - zas
Nsrcs = zas.size
fluxes = np.ones_like(azs)

# convert from alt-az to ra/dec overhead
time = Time(jd, scale='utc', format='jd')
Lat, Lon = map(float, args.array_location.split(','))
Location = EarthLocation(lat=Lat * u.degree, lon=Lon * u.degree)

source_coord = SkyCoord(alt=Angle(alts, unit=u.deg), az=Angle(azs, unit=u.deg),
                        obstime=time, frame='altaz', location=Location)

icrs_coord = source_coord.transform_to('icrs')
print("generated", len(azs), "sources")
if args.plot:
    az = np.radians(azs)
    za = np.radians(zas)
    aztmp = np.radians(aztmp)
    pl.scatter(za * np.cos(aztmp), za * np.sin(aztmp), label='Original')
    pl.legend()
    pl.show()

# print catalog
catfile = catname + ".txt"
print('writing catalog:', catfile)
outfile = open(catfile, 'w')
outstr = "#catalog generated by im_to_cal\n"
outstr += ' '.join(sys.argv) + '\n'
outstr += '#SOURCE_ID\tRA_J2000\tDec_J2000\tFlux\tFrequency\n'
for i in xrange(len(icrs_coord)):
    outstr += "TEST{i}".format(i=i) + '\t'
    outstr += str(np.round(icrs_coord[i].ra.hour, 5)) + '\t'
    outstr += str(np.round(icrs_coord[i].dec.degree, 6)) + '\t'
    outstr += '1\t'
    outstr += '100e6\n'
outfile.write(outstr)
