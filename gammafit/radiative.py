# -*- coding: utf-8 -*-
# Licensed under a 3-clause BSD style license - see LICENSE.rst
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)
import numpy as np
from .extern.validator import validate_scalar, validate_array, validate_physical_type

__all__ = ['Synchrotron', 'InverseCompton', 'PionDecay']

from astropy.extern import six
import warnings
import logging
# Get a new logger to avoid changing the level of the astropy logger
log = logging.getLogger('gammafit.radiative')
log.setLevel(logging.INFO)

# Constants and units
from astropy import units as u
# import constant values from astropy.constants
from astropy.constants import c, G, m_e, h, hbar, k_B, R_sun, sigma_sb, e, m_p
e = e.gauss

mec2 = (m_e * c ** 2).cgs

ar = (4 * sigma_sb / c).to('erg/(cm3 K4)')

heaviside = lambda x: (np.sign(x) + 1) / 2.

def _validate_ene(ene):
    from astropy.table import Table

    if isinstance(ene, dict) or isinstance(ene, Table):
        try:
            ene = validate_array('energy',u.Quantity(ene['energy']),physical_type='energy')
        except KeyError:
            raise TypeError('Table or dict does not have \'energy\' column')
    else:
        if not isinstance(ene,u.Quantity):
            ene = u.Quantity(ene)
        validate_physical_type('energy',ene,physical_type='energy')

    return ene

class Synchrotron(object):
    """Synchrotron emission from an electron population.

    Parameters
    ----------
    particle_distribution : function
        Particle distribution function, taking the electron energy in units of
        TeV and returning the particle energy density in units of number of
        electrons per TeV.

    B : :class:`~astropy.units.Quantity` float instance, optional
        Isotropic magnetic field strength. Default: equipartition
        with CMB (3.24e-6 G)
    """
    def __init__(self, particle_distribution, B=3.24e-6*u.G, **kwargs):
        self.particle_distribution = particle_distribution
        self.B = validate_scalar('B',B,physical_type='magnetic flux density')
        self.__dict__.update(**kwargs)

    def _nelec(self):
        self.log10gmin = 4
        self.log10gmax = 10
        self.ngamd = 100
        self.gam = np.logspace(self.log10gmin,self.log10gmax,
                self.ngamd*self.log10gmax/self.log10gmin)

        self.nelec = self.particle_distribution(self.gam * mec2.to('TeV'))

    def flux(self,photon_energy):
        """Compute differential synchrotron spectrum for energies in ``photon_energy``

        Compute synchrotron for random magnetic field according to approximation of
        Aharonian, Kelner, and Prosekin 2010.

        Parameters
        ----------
        photon_energy : :class:`~astropy.units.Quantity` instance
            Photon energy array.
        """

        outspecene = _validate_ene(photon_energy)

        from scipy.special import cbrt

        self._nelec()

        def Gtilde(x):
            """
            AKP10 Eq. D7

            Factor ~2 performance gain in using cbrt(x)**n vs x**(n/3.)
            """
            gt1 = 1.808 * cbrt(x) / np.sqrt(1 + 3.4 * cbrt(x) ** 2.)
            gt2 = 1 + 2.210 * cbrt(x) ** 2. + 0.347 * cbrt(x) ** 4.
            gt3 = 1 + 1.353 * cbrt(x) ** 2. + 0.217 * cbrt(x) ** 4.
            return gt1 * (gt2 / gt3) * np.exp(-x)

        log.debug('calc_sy: Starting synchrotron computation with AKB2010...')

        # strip units, ensuring correct conversion
        # astropy units do not convert correctly for gyroradius calculation when using
        # cgs (SI is fine, see https://github.com/astropy/astropy/issues/1687)
        CS1_0 = np.sqrt(3) * e.value ** 3 * self.B.to('G').value
        CS1_1 = (2 * np.pi * m_e.cgs.value * c.cgs.value ** 2 *
                 hbar.cgs.value * outspecene.to('erg').value)
        CS1 = CS1_0/CS1_1

        # Critical energy, erg
        Ec = 3 * e.value * hbar.cgs.value * self.B.to('G').value * self.gam ** 2
        Ec /= 2 * (m_e * c).cgs.value

        EgEc = outspecene.to('erg').value / np.vstack(Ec)
        dNdE = CS1 * Gtilde(EgEc)
        # return units
        spec = np.trapz(np.vstack(self.nelec) * dNdE, self.gam, axis=0) / u.s / u.erg

        return spec.to('1/(s eV)')

    def sed(self,photon_energy):
        """Compute differential synchrotron spectrum for energies in ``photon_energy``

        Compute synchrotron for random magnetic field according to approximation of
        Aharonian, Kelner, and Prosekin 2010.

        Parameters
        ----------
        photon_energy : :class:`~astropy.units.Quantity` instance
            Photon energy array.
        """

        outspecene = _validate_ene(photon_energy)
        spec = self.flux(outspecene)

        return (spec * outspecene ** 2.).to('erg/s')

class InverseCompton(object):
    """Synchrotron emission from an electron population.

    Parameters
    ----------
    particle_distribution : function
        Particle distribution function, taking the electron energy in units of
        TeV and returning the particle energy density in units of number of
        electrons per TeV.

    seed_photon_fields : string or iterable of strings (optional)
        A list of gray-body seed photon fields to use for IC calculation.
        Each of the items of the iterable can be:

        * A string equal to ``CMB`` (default), ``NIR``, or ``FIR``, for which
          radiation fields with temperatures of 2.72 K, 70 K, and 5000 K, and
          energy densities of 0.261, 0.5, and 1 eV/cm³ will be used

        * A list of length three composed of:

            1. A name for the seed photon field
            2. Its temperature as a :class:`~astropy.units.Quantity` float
               instance.
            3. Its photon field energy density as a
               :class:`~astropy.units.Quantity` float instance. If the photon
               field energy density if set to 0, its blackbody energy density
               will be computed through the Stefan-Boltzman law.
    """

    def __init__(self, particle_distribution, seed_photon_fields=['CMB',], **kwargs):
        self.particle_distribution = particle_distribution
        self.seed_photon_fields = seed_photon_fields
        self._process_input_seed()
        self.__dict__.update(**kwargs)

    def _process_input_seed(self):
        """
        take input list of seed_photon_fields and fix them into usable format
        """

        Tcmb = 2.72548 * u.K  # 0.00057 K
        Tfir = 70 * u.K
        ufir = 0.2 * u.eV / u.cm ** 3
        Tnir = 5000 * u.K
        unir = 0.2 * u.eV / u.cm ** 3

        # Allow for seed_photon_fields definitions of the type 'CMB-NIR-FIR' or 'CMB'
        if type(self.seed_photon_fields) != list:
            self.seed_photon_fields = self.seed_photon_fields.split('-')

        self.seeduf = {}
        self.seedT = {}
        for idx, inseed in enumerate(self.seed_photon_fields):
            if isinstance(inseed, six.string_types):
                if inseed == 'CMB':
                    self.seedT[inseed] = Tcmb
                    self.seeduf[inseed] = 1.0
                elif inseed == 'FIR':
                    self.seedT[inseed] = Tfir
                    self.seeduf[inseed] = (ufir / (ar * Tfir ** 4)).decompose()
                elif inseed == 'NIR':
                    self.seedT[inseed] = Tnir
                    self.seeduf[inseed] = (unir / (ar * Tnir ** 4)).decompose()
                else:
                    log.warning('Will not use seed {0} because it is not '
                                'CMB, FIR or NIR'.format(inseed))
                    raise TypeError
            elif type(inseed) == list and len(inseed) == 3:
                name, T, uu = inseed
                validate_scalar('{0}-T'.format(name), T, domain='positive',
                                physical_type='temperature')
                self.seed_photon_fields[idx] = name
                self.seedT[name] = T
                if uu == 0:
                    self.seeduf[name] = 1.0
                else:
                    validate_scalar(
                        '{0}-u'.format(name), uu, domain='positive',
                        physical_type='pressure')  # pressure has same physical type as energy density
                    self.seeduf[name] = (uu / (ar * T ** 4)).decompose()
            else:
                log.warning(
                    'Unable to process seed photon field: {0}'.format(inseed))
                raise TypeError

    def _nelec(self):
        self.log10gmin = 4
        self.log10gmax = 10.5
        self.ngamd = 300
        self.gam = np.logspace(self.log10gmin,self.log10gmax,
                self.ngamd*self.log10gmax/self.log10gmin)

        self.nelec = self.particle_distribution(self.gam * mec2)

    def _calc_specic(self, seed, outspecene):
        log.debug(
            '_calc_specic: Computing IC on {0} seed photons...'.format(seed))

        def iso_ic_on_planck(electron_energy,
                             soft_photon_temperature, gamma_energy):
            """
            IC cross-section for isotropic interaction with a blackbody
            photon spectrum following Khangulyan, Aharonian, and Kelner 2013
            (arXiv:1310.7971).

            `electron_energy` and `gamma_energy` are in units of m_ec^2
            `soft_photon_temperature` is in units of K
            """
            Ktomec2 = 1.6863699549e-10
            soft_photon_temperature *= Ktomec2

            def g(x, a):
                tmp = 1 + a[2] * x ** a[3]
                tmp2 = a[0] * x ** a[1] / tmp + 1.
                return 1. / tmp2
            gamma_energy = np.vstack(gamma_energy)
            a3 = [0.192, 0.448, 0.546, 1.377]
            a4 = [1.69, 0.549, 1.06, 1.406]
            z = gamma_energy / electron_energy
            x = z / (1 - z) / (4. * electron_energy * soft_photon_temperature)
            tmp = 1.644934 * x
            F = (1.644934 + tmp) / (1. + tmp) * np.exp(-x)
            cross_section = F * (z ** 2 / (2 * (1 - z)) * g(x, a3) + g(x, a4))
            tmp = (soft_photon_temperature / electron_energy) ** 2
            tmp *= 2.6433905738281024e+16
            cross_section = tmp * cross_section
            cc = ((gamma_energy < electron_energy) * (electron_energy > 1))
            return np.where(cc, cross_section,
                            np.zeros_like(cross_section))

        uf = self.seeduf[seed]
        T = self.seedT[seed]

        Eph = (outspecene / mec2).cgs.value
        # Catch numpy RuntimeWarnings of overflowing exp (which are then discarded anyway)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            gamint = iso_ic_on_planck(self.gam, T.to('K').value, Eph)
        lum = uf * Eph * np.trapz(self.nelec * gamint, self.gam)
        lum *= u.Unit('1/s')

        return lum / outspecene  # return differential spectrum in 1/s/eV

    def flux(self,photon_energy):
        """Compute differential IC spectrum for energies in ``photon_energy``.

        Compute IC spectrum using IC cross-section for isotropic interaction
        with a blackbody photon spectrum following Khangulyan, Aharonian, and
        Kelner 2013 (arXiv:1310.7971).

        Parameters
        ----------
        photon_energy : :class:`~astropy.units.Quantity` instance
            Photon energy array.
        """
        outspecene = _validate_ene(photon_energy)

        self._nelec()

        self.specic = np.zeros(len(outspecene)) * u.Unit('1/(s eV)')

        for seed in self.seed_photon_fields:
            # Call actual computation, detached to allow changes in subclasses
            self.specic += self._calc_specic(seed,outspecene).to('1/(s eV)')

        return self.specic.to('1/(s eV)')


    def sed(self,photon_energy):
        """Compute IC spectral energy distribution for energies in ``photon_energy``.

        Compute IC spectrum using IC cross-section for isotropic interaction
        with a blackbody photon spectrum following Khangulyan, Aharonian, and
        Kelner 2013 (arXiv:1310.7971).

        Parameters
        ----------
        photon_energy : :class:`~astropy.units.Quantity` instance
            Photon energy array.
        """
        outspecene = _validate_ene(photon_energy)

        specic = self.flux(outspecene)

        return (specic * outspecene ** 2).to('erg/s')


class PionDecay(object):
    r"""Pion decay gamma-ray emission from a proton population.

    Compute gamma-ray spectrum arising from the interaction of a relativistic
    proton distribution with stationary target protons.

    Parameters
    ----------
    particle_distribution : function
        Particle distribution function, taking proton energies in units of TeV.

    References
    ----------
    Kelner, S.R., Aharonian, F.A., and Bugayov, V.V., 2006 PhysRevD 74, 034018 [KAB06]

    """

    def __init__(self, particle_distribution, **kwargs):
        self.particle_distribution = particle_distribution
        self.__dict__.update(**kwargs)

    def _Fgamma(self, x, Ep):
        """
        KAB06 Eq.58

        Note: Quantities are not used in this function

        Parameters
        ----------
        x : float
            Egamma/Eprot
        Ep : float
            Eprot [TeV]
        """
        L = np.log(Ep)
        B = 1.30 + 0.14 * L + 0.011 * L ** 2  # Eq59
        beta = (1.79 + 0.11 * L + 0.008 * L ** 2) ** -1  # Eq60
        k = (0.801 + 0.049 * L + 0.014 * L ** 2) ** -1  # Eq61
        xb = x ** beta

        F1 = B * (np.log(x) / x) * ((1 - xb) / (1 + k * xb * (1 - xb))) ** 4
        F2 = 1. / np.log(x) - (4 * beta * xb) / (1 - xb) - (
            4 * k * beta * xb * (1 - 2 * xb)) / (1 + k * xb * (1 - xb))

        return F1 * F2

    def _sigma_inel(self, Ep):
        """
        Inelastic cross-section for p-p interaction. KAB06 Eq. 73, 79

        Note: Quantities are not used in this function

        Parameters
        ----------
        Ep : float
            Eprot [TeV]

        Returns
        -------
        sigma_inel : float
            Inelastic cross-section for p-p interaction [1/cm2].

        """
        L = np.log(Ep)
        sigma = 34.3 + 1.88 * L + 0.25 * L ** 2
        if Ep <= 0.1:
            Eth = 1.22e-3
            sigma *= (1 - (Eth / Ep) ** 4) ** 2 * heaviside(Ep - Eth)
        return sigma * 1e-27  # convert from mbarn to cm2

    def _photon_integrand(self, x, Egamma):
        """
        Integrand of Eq. 72
        """
        try:
            return self._sigma_inel(Egamma / x) * self.particle_distribution((Egamma / x)*u.TeV) \
                * self._Fgamma(x, Egamma / x) / x
        except ZeroDivisionError:
            return np.nan

    def _calc_specpp_hiE(self, Egamma):
        """
        Spectrum computed as in Eq. 42 for Egamma >= 0.1 TeV
        """
        # Fixed quad with n=40 is about 15 times faster and is always within
        # 0.5% of the result of adaptive quad for Egamma>0.1
        # WARNING: It also produces artifacts for steep distributions (e.g.
        # Maxwellian) at ~500 GeV. Reverting to adaptative quadrature
        # from scipy.integrate import fixed_quad
        # result=c*fixed_quad(self._photon_integrand, 0., 1., args = [Egamma,
        # ], n = 40)[0]
        from scipy.integrate import quad
        Egamma = Egamma.to('TeV').value
        specpp = c.cgs.value * quad(
            self._photon_integrand, 0., 1., args=Egamma,
            epsrel=1e-3, epsabs=0)[0]

        return specpp * u.Unit('1/(s TeV)')

    # variables for delta integrand
    _c = c.cgs.value
    _Kpi = 0.17
    _mp = (m_p * c ** 2).to('TeV').value
    _m_pi = 1.349766e-4  # TeV/c2

    def _delta_integrand(self, Epi):
        Ep0 = self._mp + Epi / self._Kpi
        qpi = self._c * \
            (self.nhat / self._Kpi) * self._sigma_inel(Ep0) * self.particle_distribution(Ep0*u.TeV)
        return qpi / np.sqrt(Epi ** 2 + self._m_pi ** 2)

    def _calc_specpp_loE(self, Egamma):
        """
        Delta-functional approximation for low energies Egamma < 0.1 TeV
        """
        from scipy.integrate import quad
        Egamma = Egamma.to('TeV').value
        Epimin = Egamma + self._m_pi ** 2 / (4 * Egamma)

        result = 2 * quad(self._delta_integrand, Epimin, np.inf, epsrel=1e-3,
                          epsabs=0)[0]

        return result * u.Unit('1/(s TeV)')

    def flux(self,photon_energy):
        """
        Compute differential spectrum from pp interactions using Eq. 71 and Eq.58 of KAB06.

        Parameters
        ----------
        photon_energy : :class:`~astropy.units.Quantity` instance
            Photon energy array.
        """

        outspecene = _validate_ene(photon_energy)
        from scipy.integrate import quad

        # Before starting, show total proton energy above threshold
        Eth = 1.22e-3
        Wp = quad(lambda x: x * self.particle_distribution(x*u.TeV), Eth, np.Inf)[0] * u.TeV
        self.Wp = Wp.to('erg') / u.cm**5
        log.info('W_p(E>1.22 GeV)*[nH/4πd²] = {0:.2e}'.format(self.Wp))

        if not hasattr(self, 'Etrans'):
            # Energy at which we change from delta functional to accurate
            # calculation
            self.Etrans = 0.1 * u.TeV
        else:
            validate_scalar('Etrans', self.Etrans,
                    domain='positive', physical_type='energy')

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self.nhat = 1.  # initial value, works for index~2.1
            if np.any(outspecene < self.Etrans) and np.any(outspecene >= self.Etrans):
                # compute value of nhat so that delta functional matches accurate
                # calculation at 0.1TeV
                full = self._calc_specpp_hiE(self.Etrans)
                delta = self._calc_specpp_loE(self.Etrans)
                self.nhat *= (full / delta).decompose().value

            self.specpp = np.zeros(len(outspecene)) * u.Unit('1/(s TeV)')

            for i, Egamma in enumerate(outspecene):
                if Egamma >= self.Etrans:
                    self.specpp[i] = self._calc_specpp_hiE(Egamma)
                else:
                    self.specpp[i] = self._calc_specpp_loE(Egamma)

        return self.specpp.to('1/(s eV)')

    def sed(self,photon_energy):
        """
        Compute spectral energy distribution from pp interactions using Eq. 71 and Eq.58 of KAB06.

        Parameters
        ----------
        photon_energy : :class:`~astropy.units.Quantity` instance
            Photon energy array.
        """
        outspecene = _validate_ene(photon_energy)

        specpp = self.flux(outspecene)

        return (specpp * outspecene ** 2).to('erg/s')
