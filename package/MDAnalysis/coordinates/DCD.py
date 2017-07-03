# -*- Mode: python; tab-width: 4; indent-tabs-mode:nil; coding:utf-8 -*-
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#
# MDAnalysis --- http://www.mdanalysis.org
# Copyright (c) 2006-2017 The MDAnalysis Development Team and contributors
# (see the file AUTHORS for the full list of names)
#
# Released under the GNU Public Licence, v2 or any higher version
#
# Please cite your use of MDAnalysis in published work:
#
# R. J. Gowers, M. Linke, J. Barnoud, T. J. E. Reddy, M. N. Melo, S. L. Seyler,
# D. L. Dotson, J. Domanski, S. Buchoux, I. M. Kenney, and O. Beckstein.
# MDAnalysis: A Python package for the rapid analysis of molecular dynamics
# simulations. In S. Benthall and S. Rostrup editors, Proceedings of the 15th
# Python in Science Conference, pages 102-109, Austin, TX, 2016. SciPy.
#
# N. Michaud-Agrawal, E. J. Denning, T. B. Woolf, and O. Beckstein.
# MDAnalysis: A Toolkit for the Analysis of Molecular Dynamics Simulations.
# J. Comput. Chem. 32 (2011), 2319--2327, doi:10.1002/jcc.21787
#
"""DCD trajectory I/O  --- :mod:`MDAnalysis.coordinates.DCD`
============================================================

Classes to read and write DCD binary trajectories, the format used by
CHARMM, NAMD, and also LAMMPS. Trajectories can be read regardless of
system-endianness as this is auto-detected.

Generally, DCD trajectories produced by any code can be read (with the
:class:`DCDReader`) although there can be issues with the unitcell (simulation
box) representation (see :attr:`DCDReader.dimensions`). DCDs can also be
written but the :class:`DCDWriter` follows recent NAMD/VMD convention for the
unitcell but still writes AKMA time. Reading and writing these trajectories
within MDAnalysis will work seamlessly but if you process those trajectories
with other tools you might need to watch out that time and unitcell dimensions
are correctly interpreted.


See Also
--------
:mod:`MDAnalysis.coordinates.LAMMPS`
  module provides a more flexible DCD reader/writer.


.. _Issue 187:
   https://github.com/MDAnalysis/mdanalysis/issues/187


Classes
-------

.. autoclass:: DCDReader
   :inherited-members:
.. autoclass:: DCDWriter
   :inherited-members:

"""
from __future__ import absolute_import, division, print_function, unicode_literals
from six.moves import range

import os
import errno
import numpy as np
from numpy.lib.utils import deprecate
import struct
import types
import warnings

from ..core import flags
from .. import units as mdaunits  # use mdaunits instead of units to avoid a clash
from ..exceptions import NoDataError
from . import base, core
from ..lib.formats.libdcd import DCDFile
from ..lib.mdamath import triclinic_box


class DCDReader(base.ReaderBase):
    """Reader for the DCD format.

    DCD is used by NAMD, CHARMM and LAMMPS as the default trajectory format.
    The DCD file format is not well defined. In particular, NAMD and CHARMM use
    it differently. Currently, MDAnalysis tries to guess the correct **format
    for the unitcell representation** but it can be wrong. **Check the unitcell
    dimensions**, especially for triclinic unitcells (see `Issue 187`_ and
    :attr:`DCDReader.dimensions`). A second potential issue are the units of
    time which are AKMA for the :class:`DCDReader` (following CHARMM) but ps
    for NAMD. As a workaround one can employ the configurable
    :class:`MDAnalysis.coordinates.LAMMPS.DCDReader` for NAMD trajectories.

    """
    format = 'DCD'
    flavor = 'CHARMM'
    units = {'time': 'AKMA', 'length': 'Angstrom'}

    def __init__(self, filename, convert_units=True, dt=None, **kwargs):
        """
        Parameters
        ----------
        filename : str
            trajectory filename
        convert_units : bool (optional)
            convert units to MDAnalysis units
        dt : float (optional)
            overwrite time delta stored in DCD
        **kwargs : dict
            General reader arguments.


        .. versionchanged:: 0.17.0
           Changed to use libdcd.pyx library and removed the correl function
        """
        super(DCDReader, self).__init__(
            filename, convert_units=convert_units, **kwargs)
        self._file = DCDFile(self.filename)
        self.n_atoms = self._file.header['natoms']

        delta = mdaunits.convert(self._file.header['delta'],
                                 self.units['time'], 'ps')
        if dt is None:
            dt = delta * self._file.header['nsavc']
        self.skip_timestep = self._file.header['nsavc']

        self._ts_kwargs['dt'] = dt
        self.ts = self._Timestep(self.n_atoms, **self._ts_kwargs)
        frame = self._file.read()
        # reset trajectory
        if self._file.n_frames > 1:
            self._file.seek(1)
        else:
            self._file.seek(0)
        self._frame = 0
        self.ts = self._frame_to_ts(frame, self.ts)
        # these should only be initialized once
        self.ts.dt = dt
        if self.convert_units:
            self.convert_pos_from_native(self.ts.dimensions[:3])

    def close(self):
        """close reader"""
        self._file.close()

    @property
    def n_frames(self):
        """number of frames in trajectory"""
        return len(self._file)

    def _reopen(self):
        """reopen trajectory"""
        self.ts.frame = 0
        self._frame = -1
        self._file.close()
        self._file.open('r')

    def _read_frame(self, i):
        """read frame i"""
        self._frame = i - 1
        self._file.seek(i)
        return self._read_next_timestep()

    def _read_next_timestep(self, ts=None):
        """copy next frame into timestep"""
        if self._frame == self.n_frames - 1:
            raise IOError('trying to go over trajectory limit')
        if ts is None:
            # use a copy to avoid that ts always points to the same reference
            ts = self.ts.copy()
        frame = self._file.read()
        self._frame += 1
        ts = self._frame_to_ts(frame, ts)
        self.ts = ts
        return ts

    def Writer(self, filename, n_atoms=None, **kwargs):
        """Return writer for trajectory format"""
        if n_atoms is None:
            n_atoms = self.n_atoms
        return DCDWriter(
            filename,
            n_atoms=n_atoms,
            dt=self.ts.dt,
            convert_units=self.convert_units,
            **kwargs)

    def _frame_to_ts(self, frame, ts):
        """convert a dcd-frame to a mda TimeStep"""
        ts.frame = self._frame
        ts.time = ts.frame * self.ts.dt
        ts.data['step'] = self._file.tell()

        unitcell = frame.unitcell
        pi_2 = np.pi / 2
        if (-1.0 <= unitcell[1] <= 1.0) and (-1.0 <= unitcell[3] <= 1.0) and (
                -1.0 <= unitcell[4] <= 1.0):
            # This file was generated by Charmm, or by NAMD > 2.5, with the angle
            # cosines of the periodic cell angles written to the DCD file.
            # This formulation improves rounding behavior for orthogonal cells
            # so that the angles end up at precisely 90 degrees, unlike acos().
            # (changed in MDAnalysis 0.9.0 to have NAMD ordering of the angles;
            # see Issue 187) */
            alpha = 90.0 - np.arcsin(unitcell[4]) * 90.0 / pi_2
            beta = 90.0 - np.arcsin(unitcell[3]) * 90.0 / pi_2
            gamma = 90.0 - np.arcsin(unitcell[1]) * 90.0 / pi_2
        else:
            # This file was likely generated by NAMD 2.5 and the periodic cell
            # angles are specified in degrees rather than angle cosines.
            alpha = unitcell[4]
            beta = unitcell[3]
            gamma = unitcell[1]

        unitcell[4] = alpha
        unitcell[3] = beta
        unitcell[1] = gamma

        # The original unitcell is read as ``[A, gamma, B, beta, alpha, C]``
        _ts_order = [0, 2, 5, 4, 3, 1]
        uc = np.take(unitcell, _ts_order)
        # heuristic sanity check: uc = A,B,C,alpha,beta,gamma
        if np.any(uc < 0.) or np.any(uc[3:] > 180.):
            # might be new CHARMM: box matrix vectors
            H = unitcell
            e1, e2, e3 = H[[0, 1, 3]], H[[1, 2, 4]], H[[3, 4, 5]]
            uc = triclinic_box(e1, e2, e3)
        unitcell = uc

        ts.dimensions = unitcell
        ts.positions = frame.xyz

        if self.convert_units:
            self.convert_pos_from_native(ts.dimensions[:3])
            self.convert_pos_from_native(ts.positions)

        return ts

    @property
    def dimensions(self):
        """unitcell dimensions (*A*, *B*, *C*, *alpha*, *beta*, *gamma*)

        lengths *A*, *B*, *C* are in the MDAnalysis length unit (Å), and
        angles are in degrees.

        The ordering of the angles in the unitcell is the same as in recent
        versions of VMD's DCDplugin_ (2013), namely the `X-PLOR DCD format`_:
        The original unitcell is read as ``[A, gamma, B, beta, alpha, C]`` from
        the DCD file; If any of these values are < 0 or if any of the angles
        are > 180 degrees then it is assumed it is a new-style CHARMM unitcell
        (at least since c36b2) in which box vectors were recorded.


        .. warning::
           The DCD format is not well defined. Check your unit cell
           dimensions carefully, especially when using triclinic boxes.
           Different software packages implement different conventions and
           MDAnalysis is currently implementing the newer NAMD/VMD convention
           and tries to guess the new CHARMM one. Old CHARMM trajectories might
           give wrong unitcell values. For more details see `Issue 187`_.

        .. versionchanged:: 0.9.0
           Unitcell is now interpreted in the newer NAMD DCD format as ``[A,
           gamma, B, beta, alpha, C]`` instead of the old MDAnalysis/CHARMM
           ordering ``[A, alpha, B, beta, gamma, C]``. We attempt to detect the
           new CHARMM DCD unitcell format (see `Issue 187`_ for a discussion).

        .. _`X-PLOR DCD format`: http://www.ks.uiuc.edu/Research/vmd/plugins/molfile/dcdplugin.html
        .. _Issue 187: https://github.com/MDAnalysis/mdanalysis/issues/187
        .. _DCDplugin: http://www.ks.uiuc.edu/Research/vmd/plugins/doxygen/dcdplugin_8c-source.html#l00947
        """
        return self.ts.dimensions

    @property
    def dt(self):
        """timestep between frames"""
        return self.ts.dt

    def timeseries(self,
                   asel=None,
                   start=None,
                   stop=None,
                   step=None,
                   skip=None,
                   format='afc'):
        """Return a subset of coordinate data for an AtomGroup

        Parameters
        ----------
        asel : :class:`~MDAnalysis.core.groups.AtomGroup`
            The :class:`~MDAnalysis.core.groups.AtomGroup` to read the
            coordinates from. Defaults to None, in which case the full set of
            coordinate data is returned.
        start : int (optional)
            Begin reading the trajectory at frame index `start` (where 0 is the
            index of the first frame in the trajectory); the default ``None``
            starts at the beginning.
        stop : int (optional)
            End reading the trajectory at frame index `stop`-1, i.e, `stop` is
            excluded. The trajectory is read to the end with the default
            ``None``.
        step : int (optional)
            Step size for reading; the default ``None`` is equivalent to 1 and
            means to read every frame.
        format : str (optional)
            the order/shape of the return data array, corresponding
            to (a)tom, (f)rame, (c)oordinates all six combinations
            of 'a', 'f', 'c' are allowed ie "fac" - return array
            where the shape is (frame, number of atoms,
            coordinates)


        .. deprecated:: 0.16.0
           `skip` has been deprecated in favor of the standard keyword `step`.
        """
        if skip is not None:
            step = skip
            warnings.warn(
                "Skip is deprecated and will be removed in"
                "in 1.0. Use step instead.",
                category=DeprecationWarning)

        start, stop, step = self.check_slice_indices(start, stop, step)

        if asel is not None:
            if len(asel) == 0:
                raise NoDataError(
                    "Timeseries requires at least one atom to analyze")
            atom_numbers = list(asel.indices)
        else:
            atom_numbers = list(range(self.n_atoms))

        frames = self._file.readframes(
            start, stop, step, order=format, indices=atom_numbers)
        return frames.xyz


class DCDWriter(base.WriterBase):
    """DCD Writer class

    The writer follows recent NAMD/VMD convention for the unitcell but still
    writes AKMA time. The unitcell will be written as ``[A, gamma, B, beta,
    alpha, C]``

    """
    format = 'DCD'
    multiframe = True
    flavor = 'CHARMM'
    units = {'time': 'AKMA', 'length': 'Angstrom'}

    def __init__(self,
                 filename,
                 n_atoms,
                 convert_units=True,
                 step=1,
                 dt=1,
                 remarks='',
                 nsavc=1,
                 **kwargs):
        """Parameters
        ----------
        filename : str
            filename of trajectory
        n_atoms : int
            number of atoms to be written
        convert_units : bool (optional)
            convert from MDAnalysis units to format specific units
        step : int (optional)
            number of steps between frames to be written
        dt : float (optional)
            use this time step in DCD. If ``None`` guess from first written
            TimeStep
        remarks : str (optional)
            remarks to be stored in DCD. Shouldn't be more then 240 characters
        nsavc : int (optional)
            DCD usually saves ``dt`` as the integrator timestep and the
            frequency of writes in the nsavc variable separately. Unless you
            know what you are doing you don't need to touch this value.
            If you plan to use the written DCD with another tool that depends
            on this behavior you can adjust it with this variable. The DCD
            reader will then interpret the timestep between frames as ``dt *
            nsavc``.
        **kwargs : dict
            General writer arguments
        """
        self.filename = filename
        self._convert_units = convert_units
        if n_atoms is None:
            raise ValueError("n_atoms argument is required")
        self.n_atoms = n_atoms
        self._file = DCDFile(self.filename, 'w')
        self.step = step
        self.dt = dt
        dt = mdaunits.convert(dt, 'ps', self.units['time'])
        self._file.write_header(
            remarks=remarks,
            natoms=self.n_atoms,
            nsavc=nsavc,
            delta=float(dt),
            charmm=1,
            istart=0)

    def write_next_timestep(self, ts):
        """Write timestep object into trajectory.

        Parameters
        ----------
        ts: TimeStep

        See Also
        --------
        <FormatWriter>.write(AtomGroup/Universe/TimeStep)
        The normal write() method takes a more general input
        """
        xyz = ts.positions.copy()
        dimensions = ts.dimensions.copy()

        if self._convert_units:
            xyz = self.convert_pos_to_native(xyz, inplace=True)
            dimensions = self.convert_dimensions_to_unitcell(ts, inplace=True)

        # we only support writing charmm format unit cell info
        # The DCD unitcell is written as ``[A, gamma, B, beta, alpha, C]``
        _ts_order = [0, 5, 1, 4, 3, 2]
        box = np.take(dimensions, _ts_order)

        self._file.write(xyz=xyz, box=box)

    def close(self):
        """close trajectory"""
        self._file.close()

    def __del__(self):
        self.close()
