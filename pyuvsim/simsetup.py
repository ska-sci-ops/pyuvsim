# -*- mode: python; coding: utf-8 -*
# Copyright (c) 2018 Radio Astronomy Software Group
# Licensed under the 3-clause BSD License

from __future__ import absolute_import, division, print_function

import numpy as np
from numpy.lib import recfunctions
import yaml
import os
import sys
import warnings
import six
from six.moves import map, range, zip
import astropy.units as units
from astropy.time import Time
from astropy.io.votable import parse
from astropy.coordinates import Angle, SkyCoord, EarthLocation, AltAz

from pyuvdata import UVBeam, UVData
import pyuvdata.utils as uvutils

from .source import Source
from .analyticbeam import AnalyticBeam
from .mpi import get_rank
from .utils import check_file_exists_and_increment
from pyuvsim.data import DATA_PATH as SIM_DATA_PATH


def _parse_layout_csv(layout_csv):
    """ Interpret the layout csv file """

    with open(layout_csv, 'r') as fhandle:
        header = fhandle.readline()

    header = [h.strip() for h in header.split()]
    if six.PY2:
        str_format_code = 'a'
    else:
        str_format_code = 'U'
    dt = np.format_parser([str_format_code + '10', 'i4', 'i4', 'f8', 'f8', 'f8'],
                          ['name', 'number', 'beamid', 'e', 'n', 'u'], header)

    return np.genfromtxt(layout_csv, autostrip=True, skip_header=1,
                         dtype=dt.dtype)


def array_to_sourcelist(catalog_table, lst_array=None, time_array=None, latitude_deg=None, horizon_buffer=0.04364, min_flux=None, max_flux=None):
    """
    Generate list of source.Source objects from a recarray, performming flux and horizon selections.

    Args:
        catalog_table: recarray of source catalog information. Must have columns with names
                       'source_id', 'ra_j2000', 'dec_j2000', 'flux_density_I', 'frequency'
        lst_array: For coarse RA horizon cuts, lsts used in the simulation [radians]
        time_array: Array of (float) julian dates corresponding with lst_array
        latitude_deg: Latitude of telescope in degrees. Used for declination coarse horizon cut.
        horizon_buffer: Angle (float, in radians) of buffer for coarse horizon cut. Default is about 10 minutes of sky rotation.
                        Sources whose calculated altitude is less than -horizon_buffer are excluded by the catalog read.

                        Caution! The altitude calculation does not account for precession/nutation of the Earth. The buffer angle
                        is needed to ensure that the horizon cut doesn't exclude sources near but above the horizon.
                        Since the cutoff is done using lst, and the lsts are calculated with astropy, the required buffer should
                        _not_ drift with time since the J2000 epoch. The default buffer has been tested around julian date 2457458.0.

        min_flux: Minimum stokes I flux to select [Jy]
        max_flux: Maximum stokes I flux to select [Jy]
    """

    sourcelist = []
    if len(catalog_table.shape) == 0:
        catalog_table = catalog_table.reshape((1))
    Nsrcs = catalog_table.shape[0]
    coarse_kwds = [lst_array, latitude_deg]
    coarse_horizon_cut = all([k is not None for k in coarse_kwds])
    if (not coarse_horizon_cut) and any([k is not None for k in coarse_kwds]):
        warnings.warn("It looks like you want to do a coarse horizon cut, but you're missing keywords!")

    if coarse_horizon_cut:
        lat_rad = np.radians(latitude_deg)
        buff = horizon_buffer
    for (source_id, ra_j2000, dec_j2000, flux_I, freq) in catalog_table:
        if min_flux:
            if (flux_I < min_flux):
                continue
        if max_flux:
            if (flux_I > max_flux):
                continue
        ra = Angle(ra_j2000, units.deg)
        dec = Angle(dec_j2000, units.deg)
        rise_lst = None
        set_lst = None
        if coarse_horizon_cut:
            # Identify circumpolar sources and unrising sources.
            tans = np.tan(lat_rad) * np.tan(dec.rad)
            if tans < -1:
                continue   # Source doesn't rise.
            circumpolar = tans > 1

            if not circumpolar:
                rise_lst = ra.rad - np.arccos((-1) * tans)
                set_lst = ra.rad + np.arccos((-1) * tans)
                rise_lst -= buff
                set_lst += buff

                if rise_lst < 0:
                    rise_lst += 2 * np.pi
                if set_lst > 2 * np.pi:
                    set_lst -= 2 * np.pi

        source = Source(source_id, ra,
                        dec,
                        freq=freq * units.Hz,
                        stokes=np.array([flux_I, 0., 0., 0.]),
                        rise_lst=rise_lst,
                        set_lst=set_lst)

        sourcelist.append(source)

    return np.array(sourcelist)


def read_votable_catalog(gleam_votable, input_uv=None, source_select_kwds={}):
    """
    Creates a list of pyuvsim source objects from a votable catalog.

    Args:
        gleam_votable: Path to votable catalog file.
        input_uv: The UVData object for the simulation (needed for horizon cuts)
        source_select_kwds: Dictionary of keywords for source selection
            Valid options:
            |  lst_array: For coarse RA horizon cuts, lsts used in the simulation [radians]
            |  time_array: Array of (float) julian dates corresponding with lst_array
            |  latitude_deg: Latitude of telescope in degrees. Used for declination coarse horizon cut.
            |  horizon_buffer: Angle (float, in radians) of buffer for coarse horizon cut. Default is about 10 minutes of sky rotation.
            |                  (See caveats in simsetup.array_to_sourcelist docstring)
            |  min_flux: Minimum stokes I flux to select [Jy]
            |  max_flux: Maximum stokes I flux to select [Jy]

    Returns:
        List of pyuvsim.Source objects

    Tested on: GLEAM EGC catalog, version 2
    """
    class Found(Exception):
        pass

    resources = parse(gleam_votable).resources
    try:
        for rs in resources:
            for tab in rs.tables:
                if 'GLEAM' in tab.array.dtype.names:
                    raise Found
    except Found:
        table = tab

    data = table.array
    sourcelist = []
    with np.warnings.catch_warnings():
        np.warnings.filterwarnings('ignore', '', FutureWarning)
        data = np.copy(data[['GLEAM', 'RAJ2000', 'DEJ2000', 'Fintwide']])
    Nsrcs = data.shape[0]
    freq = 200e6
    data = recfunctions.append_fields(data, 'frequency', np.ones(Nsrcs) * freq)
    data.dtype.names = ('source_id', 'ra_j2000', 'dec_j2000', 'flux_density_I', 'frequency')
    if input_uv:
        lst_array, inds = np.unique(input_uv.lst_array, return_index=True)
        time_array = input_uv.time_array[inds]
        latitude = input_uv.telescope_location_lat_lon_alt_degrees[0]
    else:
        lst_array = None
        latitude = None
    sourcelist = array_to_sourcelist(data, lst_array=lst_array,
                                     latitude_deg=latitude,
                                     **source_select_kwds)

    return sourcelist


def read_text_catalog(catalog_csv, input_uv=None, source_select_kwds={}):
    """
    Read in a text file of sources.

    Args:
        catalog_csv: csv file with the following expected columns:
            |  Source_ID: source id as a string of maximum 10 characters
            |  ra_j2000: right ascension at J2000 epoch, in decimal degrees
            |  dec_j2000: declination at J2000 epoch, in decimal degrees
            |  flux_density_I: Stokes I flux density in Janskys
            |  frequency: reference frequency (for future spectral indexing) [Hz]
                For now, all sources are flat spectrum.
        input_uv: The UVData object for the simulation (needed for horizon cuts)
        source_select_kwds: Dictionary of keywords for source selection.
            Valid options:
            |  lst_array: For coarse RA horizon cuts, lsts used in the simulation [radians]
            |  time_array: Array of (float) julian dates corresponding with lst_array
            |  latitude_deg: Latitude of telescope in degrees. Used for declination coarse horizon cut.
            |  horizon_buffer: Angle (float, in radians) of buffer for coarse horizon cut. Default is about 10 minutes of sky rotation.
            |                  (See caveats in simsetup.array_to_sourcelist docstring)
            |  min_flux: Minimum stokes I flux to select [Jy]
            |  max_flux: Maximum stokes I flux to select [Jy]

    Returns:
        List of pyuvsim.Source objects
    """
    with open(catalog_csv, 'r') as cfile:
        header = cfile.readline()
    header = [h.strip() for h in header.split() if not h[0] == '[']  # Ignore units in header
    dt = np.format_parser(['U10', 'f8', 'f8', 'f8', 'f8'],
                          ['source_id', 'ra_j2000', 'dec_j2000', 'flux_density_I', 'frequency'], header)

    catalog_table = np.genfromtxt(catalog_csv, autostrip=True, skip_header=1,
                                  dtype=dt.dtype)

    if input_uv:
        lst_array, inds = np.unique(input_uv.lst_array, return_index=True)
        time_array = input_uv.time_array[inds]
        latitude = input_uv.telescope_location_lat_lon_alt_degrees[0]
    else:
        lst_array = None
        latitude = None
        time_array = None
    sourcelist = array_to_sourcelist(catalog_table, lst_array=lst_array,
                                     latitude_deg=latitude,
                                     **source_select_kwds)

    return sourcelist


def write_catalog_to_file(filename, catalog):
    """
    Writes out a catalog to a text file, readable with simsetup.read_catalog_text()

    Args:
        filename: Path to output file (string)
        catalog: List of pyuvsim.Source objects
    """
    with open(filename, 'w+') as fo:
        fo.write("SOURCE_ID\tRA_J2000 [deg]\tDec_J2000 [deg]\tFlux [Jy]\tFrequency [Hz]\n")
        for src in catalog:
            fo.write("{}\t{:f}\t{:f}\t{:0.2f}\t{:0.2f}\n".format(src.name, src.ra.deg, src.dec.deg, src.stokes[0], src.freq.to("Hz").value))


def create_mock_catalog(time, arrangement='zenith', array_location=None, Nsrcs=None,
                        alt=None, save=False, min_alt=None, rseed=None):
    """
    Create a mock catalog.

    Sources are defined in an AltAz frame at the given time, then returned in
    ICRS ra/dec coordinates.

    Args:
        time (float or astropy Time object): Julian date
        arrangement (str): Point source pattern (default = 1 source at zenith).
            Accepted arrangements:
                |  `triangle`:  Three point sources forming a triangle around the zenith
                |  `cross`: An asymmetric cross
                |  `zenith`: Some number of sources placed at the zenith.
                |  `off-zenith`:  A single source off zenith
                |  `long-line`:  Horizon to horizon line of point sources
                |  `hera_text`:  Spell out HERA around the zenith
                |  `random`:  Randomly distributed point sources near zenith
        Nsrcs (int):  Number of sources to put at zenith
        array_location (EarthLocation object): [Default = HERA site]
        alt (float): For off-zenith and triangle arrangements, altitude to place sources. (deg)
        min_alt (float): For random and long-line arrangements, minimum altitude at which to place sources. (deg)
        save (bool): Save mock catalog as npz file.
        rseed (int): If using the random configuration, pass in a RandomState seed.

    Returns:
        catalog: List of pyuvsim.Source objects
        mock_kwds: (dictionary) The keywords defining this source catalog
    """

    if not isinstance(time, Time):
        time = Time(time, scale='utc', format='jd')

    if array_location is None:
        array_location = EarthLocation(lat='-30d43m17.5s', lon='21d25m41.9s',
                                       height=1073.)
    freq = (150e6 * units.Hz)

    if arrangement not in ['off-zenith', 'zenith', 'cross', 'triangle', 'long-line', 'hera_text', 'random']:
        raise KeyError("Invalid mock catalog arrangement: " + str(arrangement))

    mock_keywords = {'time': time.jd, 'arrangement': arrangement,
                     'array_location': repr((array_location.lat.deg, array_location.lon.deg, array_location.height.value))}

    if arrangement == 'off-zenith':
        if alt is None:
            alt = 85.0  # Degrees
        mock_keywords['alt'] = alt
        Nsrcs = 1
        alts = [alt]
        azs = [90.]   # 0 = North pole, 90. = East pole
        fluxes = [1.0]

    if arrangement == 'triangle':
        Nsrcs = 3
        if alt is None:
            alt = 87.0  # Degrees
        mock_keywords['alt'] = alt
        alts = [alt, alt, alt]
        azs = [0., 120., 240.]
        fluxes = [1.0, 1.0, 1.0]

    if arrangement == 'cross':
        Nsrcs = 4
        alts = [88., 90., 86., 82.]
        azs = [270., 0., 90., 135.]
        fluxes = [5., 4., 1.0, 2.0]

    if arrangement == 'zenith':
        if Nsrcs is None:
            Nsrcs = 1
        mock_keywords['Nsrcs'] = Nsrcs
        alts = np.ones(Nsrcs) * 90.
        azs = np.zeros(Nsrcs, dtype=float)
        fluxes = np.ones(Nsrcs) * 1 / Nsrcs
        # Divide total Stokes I intensity among all sources
        # Test file has Stokes I = 1 Jy

    if arrangement == 'random':
        if Nsrcs is None:
            Nsrcs = 1
        if min_alt is None:
            min_alt = 30  # Degrees
        mock_keywords['Nsrcs'] = Nsrcs
        np.random.seed(seed=rseed)
        mock_keywords['rseed'] = np.random.get_state()[1][0]
        alts = np.random.uniform(min_alt, 90, Nsrcs)
        azs = np.random.uniform(0, 2 * np.pi, Nsrcs)
        fluxes = np.ones(Nsrcs, dtype=float)

    if arrangement == 'long-line':
        if Nsrcs is None:
            Nsrcs = 10
        if min_alt is None:
            min_alt = 5
        mock_keywords['Nsrcs'] = Nsrcs
        mock_keywords['alt'] = alt
        fluxes = np.ones(Nsrcs, dtype=float)
        if Nsrcs % 2 == 0:
            length = 180 - min_alt * 2
            spacing = length / (Nsrcs - 1)
            max_alt = 90. - spacing / 2
            alts = np.linspace(min_alt, max_alt, Nsrcs // 2)
            alts = np.append(alts, np.flip(alts, axis=0))
            azs = np.append(np.zeros(Nsrcs // 2, dtype=float) + 180.,
                            np.zeros(Nsrcs // 2, dtype=float))
        else:
            alts = np.linspace(min_alt, 90, (Nsrcs + 1) // 2)
            alts = np.append(alts, np.flip(alts[1:], axis=0))
            azs = np.append(np.zeros((Nsrcs + 1) // 2, dtype=float) + 180.,
                            np.zeros((Nsrcs - 1) // 2, dtype=float))

    if arrangement == 'hera_text':

        azs = np.array([-254.055, -248.199, -236.310, -225.000, -206.565,
                        -153.435, -123.690, -111.801, -105.945, -261.870,
                        -258.690, -251.565, -135.000, -116.565, -101.310,
                        -98.130, 90.000, 90.000, 90.000, 90.000, 90.000,
                        -90.000, -90.000, -90.000, -90.000, -90.000,
                        -90.000, 81.870, 78.690, 71.565, -45.000, -71.565,
                        -78.690, -81.870, 74.055, 68.199, 56.310, 45.000,
                        26.565, -26.565, -45.000, -56.310, -71.565])

        zas = np.array([7.280, 5.385, 3.606, 2.828, 2.236, 2.236, 3.606,
                        5.385, 7.280, 7.071, 5.099, 3.162, 1.414, 2.236,
                        5.099, 7.071, 7.000, 6.000, 5.000, 3.000, 2.000,
                        1.000, 2.000, 3.000, 5.000, 6.000, 7.000, 7.071,
                        5.099, 3.162, 1.414, 3.162, 5.099, 7.071, 7.280,
                        5.385, 3.606, 2.828, 2.236, 2.236, 2.828, 3.606, 6.325])

        alts = 90. - zas
        Nsrcs = alts.size
        fluxes = np.ones_like(alts)

    catalog = []

    source_coord = SkyCoord(alt=Angle(alts, unit=units.deg), az=Angle(azs, unit=units.deg),
                            obstime=time, frame='altaz', location=array_location)
    icrs_coord = source_coord.transform_to('icrs')

    ra = icrs_coord.ra
    dec = icrs_coord.dec
    for si in range(Nsrcs):
        catalog.append(Source('src' + str(si), ra[si], dec[si], freq, [fluxes[si], 0, 0, 0]))
    if get_rank() == 0 and save:
        np.savez('mock_catalog_' + arrangement, ra=ra.rad, dec=dec.rad, alts=alts, azs=azs, fluxes=fluxes)

    catalog = np.array(catalog)
    return catalog, mock_keywords


def initialize_catalog_from_params(obs_params, input_uv=None):
    """
    Make catalog from parameter file specifications.

    Args:
        obs_params: Either an obsparam file name or a dictionary of parameters.
        input_uv: (UVData object) Needed to know location and time for mock catalog
                  and for horizon cuts
        min_flux: Minimum flux (in Jy) of sources to select (Default: No selection)

    Returns:
        catalog: array of Source objects.
        source_list_name: (str) Catalog identifier for metadata.
    """
    if input_uv is not None and not isinstance(input_uv, UVData):
        raise TypeError("input_uv must be UVData object")

    if isinstance(obs_params, str):
        with open(obs_params, 'r') as pfile:
            param_dict = yaml.safe_load(pfile)

        param_dict['config_path'] = os.path.dirname(obs_params)
    else:
        param_dict = obs_params

    # Parse source selection options
    catalog_select_keywords = ['min_flux', 'max_flux', 'horizon_buffer']
    catalog_params = {}

    source_params = param_dict['sources']
    if 'catalog' in source_params:
        catalog = source_params['catalog']
    else:
        raise KeyError("No catalog defined.")

    # Put catalog selection cuts in source section.
    for key in catalog_select_keywords:
        if key in source_params:
            catalog_params[key] = float(source_params[key])
    if catalog == 'mock':
        mock_keywords = {'arrangement': source_params['mock_arrangement']}
        extra_mock_kwds = ['time', 'Nsrcs', 'zen_ang', 'save', 'min_alt', 'array_location']
        for k in extra_mock_kwds:
            if k in source_params.keys():
                if k == 'array_location':
                    # String -- lat, lon, alt in degrees
                    latlonalt = [float(s.strip()) for s in source_params[k].split(',')]
                    lat, lon, alt = latlonalt
                    mock_keywords[k] = EarthLocation.from_geodetic(lon, lat, alt)
                else:
                    mock_keywords[k] = source_params[k]

        # time, arrangement, array_location, save, Nsrcs, min_alt

        if 'array_location' not in mock_keywords:
            if input_uv is not None:
                mock_keywords['array_location'] = EarthLocation.from_geocentric(*input_uv.telescope_location, unit='m')
            else:
                warnings.warn("No array_location specified. Defaulting to the HERA site.")
        if 'time' not in mock_keywords:
            if input_uv is not None:
                mock_keywords['time'] = input_uv.time_array[0]
                warnings.warn("Warning: No julian date given for mock catalog. Defaulting to first time step.")
            else:
                raise ValueError("input_uv must be supplied if using mock catalog without specified julian date")

        time = mock_keywords.pop('time')

        catalog, mock_keywords = create_mock_catalog(time, **mock_keywords)
        mock_keyvals = [str(key) + str(val) for key, val in six.iteritems(mock_keywords)]
        source_list_name = 'mock_' + "_".join(mock_keyvals)
    elif isinstance(catalog, str):
        source_list_name = os.path.basename(catalog)
        if not os.path.isfile(catalog):
            catalog = os.path.join(param_dict['config_path'], catalog)
        if catalog.endswith("txt"):
            catalog = read_text_catalog(catalog, input_uv=input_uv, source_select_kwds=catalog_params)
        elif catalog.endswith('vot'):
            catalog = read_votable_catalog(catalog, input_uv=input_uv, source_select_kwds=catalog_params)

    return np.array(catalog), source_list_name


def initialize_uvdata_from_params(obs_params):
    """
    Construct a uvdata object from parameters in a valid yaml file.

    Sufficient information must be provided by the parameters to define time and frequency arrays
    and verify the channel widths and time steps. This will error if insufficient or incompatible
    parameters are defined.

    The parameter dictionary may contain any valid UVData attributes as well.

    Args:
        obs_params: Either an obs_param file name or a dictionary of parameters read in.
                    Any uvdata parameters may be passed in through here.
    Returns:
        uv_obj, beam_list, beam_dict, beam_ids
    """

    if isinstance(obs_params, str):
        with open(obs_params, 'r') as pfile:
            param_dict = yaml.safe_load(pfile)

        param_dict['config_path'] = os.path.dirname(obs_params)
    else:
        param_dict = obs_params
    # Parse telescope parameters
    tele_params = param_dict['telescope']

    telescope_config_name = tele_params['telescope_config_name']
    layout_csv = tele_params['array_layout']
    if not os.path.isdir(param_dict['config_path']):
        param_dict['config_path'] = os.path.dirname(param_dict['config_path'])
        if not os.path.isdir(param_dict['config_path']):
            raise ValueError('config_path from yaml is not a directory')
    if not os.path.exists(telescope_config_name):
        telescope_config_name = os.path.join(param_dict['config_path'], telescope_config_name)
        if not os.path.exists(telescope_config_name):
            raise ValueError('telescope_config_name file from yaml does not exist')
    if not os.path.exists(layout_csv):
        layout_csv = os.path.join(param_dict['config_path'], layout_csv)
        if not os.path.exists(layout_csv):
            raise ValueError('layout_csv file from yaml does not exist')

    extra_keywords = {'obs_param_file': os.path.basename(param_dict['config_path']),
                      'telescope_config_file': tele_params['telescope_config_name'],
                      'antenna_location_file': tele_params['array_layout']}

    param_dict['extra_keywords'] = extra_keywords

    ant_layout = _parse_layout_csv(layout_csv)

    with open(telescope_config_name, 'r') as yf:
        telparam = yaml.safe_load(yf)
        tloc = telparam['telescope_location'][1:-1]  # drop parens
        tloc = list(map(float, tloc.split(",")))
        tloc[0] *= np.pi / 180.
        tloc[1] *= np.pi / 180.   # Convert to radians
        telparam['telescope_location'] = uvutils.XYZ_from_LatLonAlt(*tloc)

    param_dict.update(telparam)
    E, N, U = ant_layout['e'], ant_layout['n'], ant_layout['u']
    antnames = ant_layout['name']
    beam_ids = ant_layout['beamid']
    beam_list = []
    beam_dict = {}

    for beamID in np.unique(beam_ids):
        beam_model = telparam['beam_paths'][beamID]
        which_ants = antnames[np.where(beam_ids == beamID)]
        for a in which_ants:
            beam_dict[a] = beamID
        if beam_model in AnalyticBeam.supported_types:
            if beam_model == 'gaussian':
                try:
                    sigma = telparam['sigma']
                    beam_model = beam_model + '_' + str(sigma)
                except KeyError:
                    raise KeyError("Missing sigma for gaussian beam.")
            if beam_model == 'airy':
                try:
                    diam = telparam['diameter']
                    beam_model = beam_model + '_' + str(diam)
                except KeyError:
                    raise KeyError("Missing diameter for airy beam.")
        else:
            # If not analytic, it's a beamfits file path.
            if not os.path.exists(beam_model):
                path = os.path.join(SIM_DATA_PATH, beam_model)
                beam_model = path
                if not os.path.exists(path):
                    raise OSError("Could not find file " + beam_model)
        beam_list.append(beam_model)

    param_dict['Nants_data'] = antnames.size
    param_dict['Nants_telescope'] = antnames.size
    param_dict['antenna_names'] = np.array(antnames.tolist())
    param_dict['antenna_numbers'] = np.array(ant_layout['number'])
    antpos_enu = np.vstack((E, N, U)).T

    param_dict['antenna_positions'] = uvutils.ECEF_from_ENU(antpos_enu, *tloc) - param_dict['telescope_location']

    # Parse frequency structure
    freq_params = param_dict['freq']

    freq_keywords = ['freq_array', 'start_freq', 'end_freq', 'Nfreqs',
                     'channel_width', 'bandwidth']
    fa, sf, ef, nf, cw, bw = [fk in freq_params for fk in freq_keywords]
    kws_used = ", ".join(freq_params.keys())

    if fa:
        freq_arr = np.array(freq_params['freq_array'])
        freq_params['Nfreqs'] = freq_arr.size
        if freq_params['Nfreqs'] > 1:
            freq_params['channel_width'] = np.diff(freq_arr)[0]
        elif 'channel_width' not in freq_params:
            raise ValueError("Channel width must be specified "
                             "if freq_arr has length 1")
    else:
        if not nf:
            if not cw:
                raise ValueError("Either channel_width or Nfreqs "
                                 " must be included in parameters:" + kws_used)
            if sf and ef:
                freq_params['bandwidth'] = freq_params['end_freq'] - freq_params['start_freq']
                bw = True
            if bw:
                freq_params['Nfreqs'] = int(np.floor(freq_params['bandwidth']
                                                     / freq_params['channel_width']))
            else:
                raise ValueError("Either bandwidth or band edges "
                                 "must be specified: " + kws_used)

        if not cw:
            if not bw:
                raise ValueError("Either bandwidth or channel width"
                                 " must be specified: " + kws_used)
            freq_params['channel_width'] = (freq_params['bandwidth']
                                            / float(freq_params['Nfreqs']))

        if not bw:
            freq_params['bandwidth'] = (freq_params['channel_width']
                                        * freq_params['Nfreqs'])
            bw = True
        if not sf:
            if ef and bw:
                freq_params['start_freq'] = freq_params['end_freq'] - freq_params['bandwidth']
        if not ef:
            if sf and bw:
                freq_params['end_freq'] = freq_params['start_freq'] + freq_params['bandwidth']

        freq_arr = np.linspace(freq_params['start_freq'],
                               freq_params['end_freq'],
                               freq_params['Nfreqs'], endpoint=False)
    if freq_params['Nfreqs'] != 1:
        assert np.allclose(np.diff(freq_arr), freq_params['channel_width'] * np.ones(freq_params["Nfreqs"] - 1), atol=1.0)  # 1 Hz

    Nspws = 1 if 'Nspws' not in freq_params else freq_params['Nspws']
    freq_arr = np.repeat(freq_arr, Nspws).reshape(Nspws, freq_params['Nfreqs'])

    param_dict['channel_width'] = freq_params['channel_width']
    param_dict['freq_array'] = freq_arr
    param_dict['Nfreqs'] = freq_params['Nfreqs']

    # Parse time structure
    time_params = param_dict['time']

    time_keywords = ['start_time', 'end_time', 'Ntimes', 'integration_time',
                     'duration_hours', 'duration_days']
    st, et, nt, it, dh, dd = [tk in time_params for tk in time_keywords]
    kws_used = ", ".join(time_params.keys())
    daysperhour = 1 / 24.
    hourspersec = 1 / 60.**2
    dayspersec = daysperhour * hourspersec

    if dh and not dd:
        time_params['duration'] = time_params['duration_hours'] * daysperhour
        dd = True
    elif dd:
        time_params['duration'] = time_params['duration_days']

    if not nt:
        if not it:
            raise ValueError("Either integration_time or Ntimes must be "
                             "included in parameters:" + kws_used)
        if st and et:
            time_params['duration'] = time_params['end_time'] - time_params['start_time']
            dd = True
        if dd:
            time_params['Ntimes'] = int(np.round(time_params['duration']
                                                 / (time_params['integration_time']
                                                    * dayspersec))) + 1
        else:
            raise ValueError("Either duration or time bounds must be specified: "
                             + kws_used)

    if not it:
        if not dd:
            raise ValueError("Either duration or integration time "
                             "must be specified: " + kws_used)
        time_params['integration_time'] = (24. * 3600.) * (time_params['duration']
                                                           / float(time_params['Ntimes']))  # In seconds

    inttime_days = time_params['integration_time'] * 1 / (24. * 3600.)
    inttime_days = np.trunc(inttime_days * 24 * 3600) / (24. * 3600.)
    if not dd:
        time_params['duration'] = inttime_days * (time_params['Ntimes'])
        dd = True
    if not st:
        if et and dd:
            time_params['start_time'] = time_params['end_time'] - time_params['duration']
    if not et:
        if st and dd:
            time_params['end_time'] = time_params['start_time'] + time_params['duration']
    if not (st or et):
        raise ValueError("Either a start or end time must be specified: " + kws_used)

    time_arr = np.linspace(time_params['start_time'],
                           time_params['end_time'],
                           time_params['Ntimes'], endpoint=False)

    if time_params['Ntimes'] != 1:
        assert np.allclose(np.diff(time_arr), inttime_days * np.ones(time_params["Ntimes"] - 1), atol=1e-4)   # To nearest second

    Nbl = (param_dict['Nants_data'] + 1) * param_dict['Nants_data'] // 2
    time_arr = np.sort(np.tile(time_arr, Nbl))
    param_dict['integration_time'] = (np.ones_like(time_arr, dtype=np.float64)
                                      * time_params['integration_time'])
    param_dict['time_array'] = time_arr
    param_dict['Ntimes'] = time_params['Ntimes']
    param_dict['Nspws'] = 1
    param_dict['Npols'] = 4
    # Now make a UVData object with these settings built in.
    # The syntax below allows for other valid uvdata keywords to be passed
    #  without explicitly setting them here.

    if 'object_name' not in param_dict:
        tloc = EarthLocation.from_geocentric(*param_dict['telescope_location'], unit='m')
        time = Time(time_arr[0], scale='utc', format='jd')
        src, _ = create_mock_catalog(time, arrangement='zenith', array_location=tloc)
        src = src[0]
        source_file_name = os.path.basename(param_dict['sources']['catalog'])
        param_dict['object_name'] = '{}_ra{:.4f}_dec{:.4f}'.format(source_file_name, src.ra.deg, src.dec.deg)

    uv_obj = UVData()
    for k in param_dict:
        # use the __iter__ function on UVData to get list of UVParameters on UVData
        param_name = [getattr(uv_obj, param).name for param in uv_obj]
        if k in param_name:
            setattr(uv_obj, k, param_dict[k])

    bls = np.array([uv_obj.antnums_to_baseline(uv_obj.antenna_numbers[j], uv_obj.antenna_numbers[i])
                    for i in range(0, uv_obj.Nants_data)
                    for j in range(i, uv_obj.Nants_data)])

    uv_obj.baseline_array = np.tile(bls, uv_obj.Ntimes)
    uv_obj.Nbls = bls.size
    uv_obj.Nblts = uv_obj.Nbls * uv_obj.Ntimes

    uv_obj.ant_1_array, uv_obj.ant_2_array = \
        uv_obj.baseline_to_antnums(uv_obj.baseline_array)

    # add other required metadata to allow select to work without errors
    # these will all be overwritten in uvsim.init_uvdata_out, so it's ok to hardcode them here
    uv_obj.polarization_array = np.array([-5, -6, -7, -8])
    uv_obj.set_lsts_from_time_array()
    uv_obj.set_uvws_from_antenna_positions()
    uv_obj.history = ''

    valid_select_keys = ['antenna_nums', 'antenna_names', 'ant_str', 'bls', 'frequencies', 'freq_chans', 'times', 'polarizations', 'blt_inds']
    # down select baselines (or anything that can be passed to pyuvdata's select method)
    # Note: cannot down select polarizations (including via ant_str or bls keywords)
    if 'select' in param_dict:
        select_params = dict([(k, v) for k, v in param_dict['select'].items() if k in valid_select_keys])
        if 'polarizations' in select_params:
            raise ValueError('Can not down select on polarizations -- pyuvsim '
                             'computes all polarizations')
        if 'bls' in select_params:
            bls = select_params['bls']
            if isinstance(bls, six.string_types):
                # If read from file, this should be a string.
                bls = eval(bls)
                select_params['bls'] = bls
            if any([len(item) == 3 for item in bls]):
                raise ValueError('Only length 2 tuples allowed in bls: can not '
                                 'down select on polarizations -- pyuvsim '
                                 'computes all polarizations')
        if 'ant_str' in select_params:
            bls, polarizations = uv_obj.parse_ants(select_params['ant_str'])
            if polarizations is not None:
                raise ValueError('polarizations can not be specified in ant_str: '
                                 'can not down select on polarizations -- pyuvsim '
                                 'computes all polarizations')

        select_params['metadata_only'] = True
        uv_obj.select(**select_params)

    return uv_obj, beam_list, beam_dict, beam_ids


def uvdata_to_telescope_config(uvdata_in, beam_filepath, layout_csv_name=None,
                               telescope_config_name=None,
                               return_names=False, path_out='.'):
    """
    From a uvfits file, generate telescope parameters files.

    Output config files are written to the current directory, unless keep_path is set.

    Args:
        uvdata_in (UVData): object to process
        path_out (str): Target directory for the config file.
        beam_filepath (str): Path to a beamfits file.
        layout_csv_name (str, optional): The name for the antenna positions
            csv file (Default <telescope_name>_layout.csv)
        telescope_config_name (str, optional): The name for the telescope config file
            (Default teleconfig_#number.yaml)
        return_names (bool, optional): Return the file names for loopback tests.

    Returns:
        if return_names, returns tuple (path, telescope_config_name, layout_csv_name)
    """

    if telescope_config_name is None:
        telescope_config_path = \
            check_file_exists_and_increment(os.path.join(path_out, 'telescope_config_'
                                                         + uvdata_in.telescope_name
                                                         + '.yaml'))
        telescope_config_name = os.path.basename(telescope_config_path)

    if layout_csv_name is None:
        layout_csv_path = check_file_exists_and_increment(os.path.join(path_out, uvdata_in.telescope_name + "_layout.csv"))
        layout_csv_name = os.path.basename(layout_csv_path)

    antpos_enu, antenna_numbers = uvdata_in.get_ENU_antpos(center=False)

    e, n, u = antpos_enu.T
    beam_ids = np.zeros_like(e).astype(int)
    col_width = max([len(name) for name in uvdata_in.antenna_names])
    header = ("{:" + str(col_width) + "} {:8} {:8} {:10} {:10} {:10}\n").format("Name", "Number", "BeamID", "E", "N", "U")
    with open(os.path.join(path_out, layout_csv_name), 'w') as lfile:
        lfile.write(header + '\n')
        for i in range(beam_ids.size):
            e, n, u = antpos_enu[i]
            beam_id = beam_ids[i]
            name = uvdata_in.antenna_names[i]
            num = uvdata_in.antenna_numbers[i]
            line = ("{:" + str(col_width) + "} {:8d} {:8d} {:10.4f} {:10.4f} {:10.4f}\n").format(name, num, beam_id, e, n, u)
            lfile.write(line)

    # Write the rest to a yaml file.
    yaml_dict = dict(
        telescope_name=uvdata_in.telescope_name,
        telescope_location=repr(uvdata_in.telescope_location_lat_lon_alt_degrees),
        Nants=uvdata_in.Nants_telescope,
        beam_paths={
            0: beam_filepath
        }
    )

    with open(os.path.join(path_out, telescope_config_name), 'w+') as yfile:
        yaml.dump(yaml_dict, yfile, default_flow_style=False)

    print('Path: {}, telescope_config: {}, layout: {}'.format(path_out, telescope_config_name,
                                                              layout_csv_name))

    if return_names:
        return path_out, telescope_config_name, layout_csv_name


def uvdata_to_config_file(uvdata_in, param_filename=None, telescope_config_name='',
                          layout_csv_name='', catalog='mock', path_out='.'):
    """
    Extract simulation configuration settings from uvfits.

    Args:
        uvdata_in (UVData): uvdata object.

    Keywords:
        param_filename (str, optional): output param file name, defaults to obsparam_#.yaml.
        telescope_config_name (str, optional): Name of yaml file file. Defaults to blank string.
        layout_csv_name (str, optional): Name of layout csv file. Defaults to blank string.
        catalog (str, optional): Path to catalog file, defaults to 'mock'.
        path_out (str, optional): Where to put config files.
    """

    if param_filename is None:
        param_filename = check_file_exists_and_increment(os.path.join(path_out, 'obsparam.yaml'))
        param_filename = os.path.basename(param_filename)

    freq_array = uvdata_in.freq_array[0, :].tolist()
    time_array = uvdata_in.time_array.tolist()
    integration_time_array = np.array(uvdata_in.integration_time)
    if np.max(integration_time_array) != np.min(integration_time_array):
        warnings.warn('The integration time is not constant. Using the shortest integration time')
    integration_time = float(np.min(integration_time_array))
    param_dict = dict(
        time=dict(
            start_time=time_array[0],
            end_time=time_array[-1],
            Ntimes=uvdata_in.Ntimes,
            integration_time=integration_time,
        ),
        freq=dict(
            start_freq=freq_array[0],
            end_freq=freq_array[-1] + uvdata_in.channel_width,
            channel_width=uvdata_in.channel_width,
            Nfreqs=uvdata_in.Nfreqs,
        ),
        sources=dict(
            catalog=catalog
        ),

        telescope=dict(
            telescope_config_name=telescope_config_name,
            array_layout=layout_csv_name
        ),

        filing=dict(
            outdir='.',
            outfile_name='',
            outfile_prefix=''
        )
    )

    if catalog == 'mock':
        param_dict['sources']['mock_arrangement'] = 'zenith'

    with open(os.path.join(path_out, param_filename), 'w') as yfile:
        yaml.dump(param_dict, yfile, default_flow_style=False)


def beam_string_to_object(beam_model):
    """
        Make a beam object given an identifying string.
    """
    # Identify analytic beams
    if beam_model.startswith('gaussian'):
        sigma = float(beam_model.split("_")[1])
        return AnalyticBeam('gaussian', sigma=sigma)
    elif beam_model.startswith('airy'):
        diameter = float(beam_model.split("_")[1])
        return AnalyticBeam('airy', diameter=diameter)
    elif beam_model.startswith('uniform'):
        return AnalyticBeam('uniform')
    else:
        path = beam_model   # beam_model = path to beamfits
    uvb = UVBeam()
    uvb.read_beamfits(path)
    return uvb


def write_uvdata(uv_obj, param_dict, return_filename=False, dryrun=False, out_format=None):
    """
    Parse output file information from parameters and write uvfits to file.

    Args:
        uv_obj: UVData object to write out.
        param_dict: parameter dictionary defining output path, filename, and
                    whether or not to clobber.
        return_filename: (Default false) Return the file path
        dryrun: (Default false) Don't write to file.
        out_format: (Default uvfits) Write as uvfits/miriad/uvh5

    Returns:
        File path, if return_filename is True
    """
    if 'filing' in param_dict.keys():
        param_dict = param_dict['filing']
    if 'outdir' not in param_dict:
        param_dict['outdir'] = '.'
    if 'output_format' in param_dict:
        out_format = param_dict['output_format']
    elif out_format is None:
        out_format = 'uvfits'

    if 'outfile_name' not in param_dict or param_dict['outfile_name'] == '':
        outfile_prefix = ""
        outfile_suffix = "results"
        if 'outfile_prefix' in param_dict:
            outfile_prefix = param_dict['outfile_prefix']
        if 'outfile_suffix' in param_dict:
            outfile_suffix = param_dict['outfile_suffix']
        outfile_name = "_".join([outfile_prefix, outfile_suffix])
        outfile_name = os.path.join(param_dict['outdir'], outfile_name)
    else:
        outfile_name = os.path.join(param_dict['outdir'], param_dict['outfile_name'])
    print('Outfile path: ', outfile_name)

    if not os.path.exists(param_dict['outdir']):
        os.makedirs(param_dict['outdir'])

    if out_format == 'uvfits':
        if not outfile_name.endswith(".uvfits"):
            outfile_name = outfile_name + ".uvfits"

    if out_format == 'uvh5':
        if not outfile_name.endswith(".uvh5"):
            outfile_name = outfile_name + ".uvh5"

    noclobber = ('clobber' not in param_dict) or bool(param_dict['clobber'])
    if noclobber:
        outfile_name = check_file_exists_and_increment(outfile_name)

    if not dryrun:
        if out_format == 'uvfits':
            uv_obj.write_uvfits(outfile_name, force_phase=True, spoof_nonessential=True)
        elif out_format == 'miriad':
            uv_obj.write_miriad(outfile_name)
        elif out_format == 'uvh5':
            uv_obj.write_uvh5(outfile_name)
        else:
            raise ValueError("Invalid output format. Options are \" uvfits\" or \"miriad\"")
    if return_filename:
        return outfile_name
