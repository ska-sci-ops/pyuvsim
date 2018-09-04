# -*- mode: python; coding: utf-8 -*
# Copyright (c) 2018 Radio Astronomy Software Group
# Licensed under the 2-clause BSD License

from __future__ import absolute_import, division, print_function

import mpi4py
import sys
mpi4py.rc.initialize = False

rank = 0
Npus = 1
comm = None


def set_mpi_excepthook(mpi_comm):
    """Kill the whole job on an uncaught python exception"""

    def mpi_excepthook(exctype, value, traceback):
        sys.__excepthook__(exctype, value, traceback)
        mpi_comm.Abort(1)

    sys.excepthook = mpi_excepthook


def start_mpi():
    # Avoid accidentally doing MPI_INIT twice
    global comm, Npus, rank
    if comm is None:
        mpi4py.MPI.Init()
        comm = mpi4py.MPI.COMM_WORLD
        Npus = comm.Get_size()
        rank = comm.Get_rank()
        set_mpi_excepthook(comm)


def get_rank():
    return rank


def get_Npus():
    return Npus


def get_comm():
    return comm
