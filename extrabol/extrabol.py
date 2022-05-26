#!/usr/bin/env python

from turtle import shape, width
import numpy as np
from astroquery.svo_fps import SvoFps
import matplotlib.pyplot as plt
import george
from scipy.optimize import minimize, curve_fit
import argparse
from astropy.cosmology import Planck13 as cosmo
from astropy.cosmology import z_at_value
from astropy import units as u
import os 
from astropy.table import Table
from astropy.io import ascii
import matplotlib.cm as cm
import sys
from scipy import interpolate as interp
from george.modeling import Model
from statistics import median

epsilon = 0.0001
c = 2.99792458E10
sigsb = 5.6704e-5 #erg / cm^2 / s / K^4
h = 6.62607E-27
ang_to_cm = 1e-8
k_B = 1.38064852E-16 # cm^2 * g / s^2 / K


def bbody(lam,T,R):
    '''
    Calculate BB L_lam (adapted from superbol, Nicholl, M. 2018, RNAAS)

    Parameters
    ----------
    lam : float
        Reference wavelengths in Angstroms
    T : float
        Temperature in Kelvin
    R : float
        Radius in cm

    Output
    ------
    L_lam in erg/s/cm
    '''

    lam_cm = lam * ang_to_cm
    exponential = (h * c) / (lam_cm * k_B * T)
    blam = ((2. * np.pi * h * c ** 2) / (lam_cm ** 5)) / (np.exp(exponential) - 1.)
    area = 4. * np.pi * R**2
    lum = blam * area

    return lum

def chi_square(dat, model, uncertainty):

    chi2=0.
    for i in np.arange(len(dat)):
        chi2 += ((model[i]-dat[i])/uncertainty[i])**2.
    return chi2


def read_in_photometry(filename, dm, redshift, start, end, snr):
    '''
    Read in SN file

    Parameters
    ----------
    filename : string
        Input file name
    dm : float
        Distance modulus
    z : float
        redshift

    Output
    ------
    lc : numpy.array
        Light curve array
    wv_corr : float
        Mean of wavelengths, used in GP pre-processing
    flux_corr : float
        Flux correction, used in GP pre-processing
    my_filters : list
        List of filter names
    '''

    photometry_data = np.loadtxt(filename,dtype=str, skiprows = 2)

    phases = np.asarray(photometry_data[:,0],dtype=float)
    errs = np.asarray(photometry_data[:,2],dtype=float)
    index = SvoFps.get_filter_index(timeout=3600)
    filterIDs = np.asarray(index['filterID'].data,dtype=str)
    wavelengthEffs = np.asarray(index['WavelengthEff'].data,dtype=float)
    widthEffs = np.asarray(index['WidthEff'].data,dtype=float)
    zpts_all = np.asarray(index['ZeroPoint'].data,dtype=str)

    wv_effs = []
    width_effs = []
    my_filters = []
    for ufilt in photometry_data[:,3]:
        gind = np.where(filterIDs==ufilt)[0]
        if len(gind) == 0:
            sys.exit('Cannot find '+str(ufilt)+' in SVO.')
        wv_effs.append(wavelengthEffs[gind][0])
        width_effs.append(widthEffs[gind][0])   
        my_filters.append(ufilt) 

    zpts = []
    fluxes = []
    for datapoint in photometry_data:
        mag = float(datapoint[1]) - dm
        if datapoint[-1] == 'AB':
            zpts.append(3631.00)
        else:
            gind = np.where(filterIDs==datapoint[3])
            zpts.append(float(zpts_all[gind[0]][0]))
    
        flux = 10.** (mag / -2.5) * zpts[-1] * (1.+redshift)
        #I'm calling ths flux..but I'm going to do it in log flux space
        flux = 2.5 * (np.log10(flux) - np.log10(3631.00))
        fluxes.append(flux)

    wv_effs = np.asarray(wv_effs)

    wv_corr = np.mean(wv_effs/(1.+redshift))
    flux_corr = np.min(fluxes) - 1.0
    wv_effs = wv_effs - wv_corr
    fluxes = np.asarray(fluxes) - flux_corr

    #Eliminate any data points bellow threshold snr
    gis = []
    for i in np.arange(len(phases)):
        if (1/errs[i]) >= snr:
            gis.append(i)
    gis = np.asarray(gis, dtype = int)
    
    phases = phases[gis]
    fluxes = fluxes[gis]
    wv_effs = wv_effs[gis]
    errs = errs[gis]
    width_effs = np.asarray(width_effs) #this wasn't an array already??? idk, this makes it work though
    width_effs = width_effs[gis]
    my_filters = np.asarray(my_filters) #also wasn't an array??
    my_filters = my_filters[gis]
    #print(phases)
    #set the first acceptable data point to t=0
    phases = np.asarray(phases) - np.min(phases)

    #eliminate any data points outside of specified range (with respect to first data point)
    gis = []
    for i in np.arange(len(phases)):
        if phases[i] <= end and phases[i] >= start:
            gis.append(i)
    gis = np.asarray(gis, dtype = int)
    
    phases = phases[gis]
    fluxes = fluxes[gis]
    wv_effs = wv_effs[gis]
    errs = errs[gis]
    width_effs = width_effs[gis]
    my_filters = my_filters[gis]
    #print(phases)
    lc = np.vstack((phases,fluxes,wv_effs/1000.,errs,width_effs))

    return lc,wv_corr,flux_corr, my_filters


def generate_template(filter_wv, sn_type):
    '''
    Prepare and interpolate SN1a Template

    Parameters
    ----------
    fiter_wv : numpy.array
        effective wavelength of filters in Angstroms
    
    Output
    ------
    temp_interped : RectBivariateSpline object
        interpolated template
    '''

    template = np.load('./extrabol/template/smoothed_sn'+ sn_type +'.npz')
    temp_times = template['time']
    temp_wavelength = template['wavelength']
    temp_f_lambda = template['f_lambda']
    
    #The template is too large, so we thin it out
    #CHOP ENDS
    gis = []
    for i in np.arange(len(temp_wavelength)):
            if temp_wavelength[i] < np.amax(filter_wv) and temp_wavelength[i] > np.amin(filter_wv):
                gis.append(i)
    temp_times = temp_times[gis]
    temp_wavelength = temp_wavelength[gis]
    temp_f_lambda = temp_f_lambda[gis]
    
    #REMOVE EVERY OTHER TIME
    gis = []
    for i in np.arange(len(temp_times)):
        if temp_times[i] % 2. == 0:
            gis.append(i)
    temp_times = temp_times[gis]
    temp_wavelength = temp_wavelength[gis]
    temp_f_lambda = temp_f_lambda[gis]
    
    #REMOVE EVERY OTHER WAVELENGTH   
    gis = []
    for i in np.arange(len(temp_wavelength)):
        if temp_wavelength[i] % 20. == 0:
            gis.append(i)
    temp_times = temp_times[gis]
    temp_wavelength = temp_wavelength[gis]
    temp_f_lambda = temp_f_lambda[gis]
    
    #REMOVE INITIAL RISE (too dim = low snr)
    gis = []
    for i in np.arange(len(temp_times)):
        if temp_times[i] >= 1.:
            gis.append(i)
    temp_times = temp_times[gis]
    temp_wavelength = temp_wavelength[gis]
    temp_f_lambda = temp_f_lambda[gis]

    #RectBivariateSpline requires that x and y are 1-d arrays, strictly ascending
    temp_times_u = np.unique(temp_times)
    temp_wavelength_u = np.unique(temp_wavelength)
    temp_f_lambda_u = np.zeros((len(temp_times_u), len(temp_wavelength_u)))
    for i in np.arange(len(temp_times_u)):
        gis = np.where(temp_times == temp_times_u[i])
        temp_f_lambda_u[i,:] = temp_f_lambda[gis]
    #Template needs to be converted to log(flux) to match data
    for i in np.arange(len(temp_wavelength_u)):
        wv = temp_wavelength_u[i]
        temp_f_lambda_u[:,i] = 2.5 * np.log10((wv**2) * temp_f_lambda_u[:,i])
    
    temp_interped = interp.RectBivariateSpline(temp_times_u, temp_wavelength_u, temp_f_lambda_u)

    return temp_interped


def fit_template(wv, template_to_fit, filts, wv_corr, flux, time, errs, z, output_chi = False, output_params = True):
    '''
    Get parameters to roughly fit template to data

    Parameters
    ----------
    wv : numpy.array
        wavelenght of filters in angstroms
    template_to_fit : RectBivariateSpline object
        interpolated template
    filts : numpy.array
        normalized wavelength values for each obseration
    wv_corr : float
        Mean of wavelengths, used in GP pre-processing
    flux : numpy.array
        flux data from observations
    time : numpy.array
        time data from observations
    Output
    ------
    A_opt : float
        multiplicative constant to be applied to template flux values
    t_c_opt : float
        additive constant to line up template and data in time
    '''

    A_opt = []
    t_c_opt = []
    t_s_opt = []
    chi2 = []

    #We will fit the template to the data for each filter used, and use the median parameter values
    for wavelength in wv:

        #A callable function to test chi2 later on
        def model(time, filt, A, t_c, t_s):
            time_sorted = sorted(time)
            time_corr = np.asarray(time_sorted) * 1./t_s + t_c
            mag = template_to_fit(time_corr, filt) + A    #not really magnitudes, just log(flux) to match data
            mag = np.ndarray.flatten(mag)
            return mag

        #curve_fit won't know what to do with the filt param so I need to modify it slightly
        def curve_to_fit(time, A, t_c, t_s):
            mag = model(time, wavelength, A, t_c, t_s)
            return mag

        gis = np.where(filts * 1000 + wv_corr == wavelength)
        dat_fluxes = flux[gis]
        dat_times = time[gis]
        dat_errs = errs[gis]
        popt, pcov = curve_fit(curve_to_fit, dat_times, dat_fluxes, p0 =[20,0,1+z], maxfev = 8000, bounds = ([-np.inf, -np.inf, 0],np.inf))
        A_opt.append(popt[0])
        t_c_opt.append(popt[1])
        t_s_opt.append(popt[2])

        param_chi = 0           #test chi2 for this set of parameters
        for filt in wv:
            m = model(dat_times, filt, popt[0], popt[1], popt[2])
            param_chi += chi_square(dat_fluxes, m, dat_errs)
        chi2.append(param_chi)

    print(chi2)
    gi = np.argmin(chi2)
    chi2 = chi2[gi]
    print('Chosen chi2 for template: '+str(chi2))
    A_opt = A_opt[gi]

    t_c_opt = t_c_opt[gi]

    t_s_opt = t_s_opt[gi]
    #print('opt time stretch = '+str(t_s_opt))

    if output_chi == True:
        if output_params == True:
            return A_opt, t_c_opt, t_s_opt, chi2
        else:
            return chi2
    else:
        if output_params == False:
            return 0
        else:
            return A_opt, t_c_opt, t_s_opt

def test(lc, wv_corr, z):
    '''
    Test every available template for the lowest possible chi^2

    Parameters
    ----------
    lc : numpy.array
        LC array

    Output
    ------
    dense_lc : numpy.array
        GP-interpolated LC and errors
    '''
    lc = lc.T

    time = lc[:,0]
    flux = lc[:,1]
    filts = lc[:,2]
    errs = lc[:,3]
    ufilts = np.unique(lc[:,2])
    ufilts_in_angstrom = ufilts * 1000.0 + wv_corr

    templates = ['1a','1bc','2p','2l']
    chi2 = []
    for template in templates:
        template_to_fit = generate_template(ufilts_in_angstrom, template)
        chi2.append(fit_template(ufilts_in_angstrom, template_to_fit, filts, wv_corr, flux, time, errs, z, output_chi = True, output_params = False))

    gi = np.argmin(chi2)
    print('chi2 for all templates: ' + str(chi2))
    chi2 = chi2[gi]
    best_temp = templates[gi]
    return best_temp


def interpolate(lc, wv_corr, sn_type, use_mean, z):
    '''
    Interpolate the LC using a 2D Gaussian Process (GP)

    Parameters
    ----------
    lc : numpy.array
        LC array

    Output
    ------
    dense_lc : numpy.array
        GP-interpolated LC and errors
    '''

    lc = lc.T

    times = lc[:,0]
    fluxes = lc[:,1]
    filters = lc[:,2]
    errs = lc[:,3]
    stacked_data = np.vstack([times, filters]).T
    ufilts = np.unique(lc[:,2])
    ufilts_in_angstrom = ufilts * 1000.0 + wv_corr
    nfilts = len(ufilts)
    x_pred = np.zeros((len(lc)*nfilts, 2))
    dense_fluxes = np.zeros((len(lc), nfilts))
    dense_errs = np.zeros((len(lc), nfilts))

    test_y=[]        #only used if mean = True, but I still need it to exist either way
    test_times=[]
    if(use_mean == True):
        template = generate_template(ufilts_in_angstrom, sn_type)
        f_stretch, t_shift, t_stretch = fit_template(ufilts_in_angstrom, template, filters, wv_corr, fluxes, times, errs, z)
        #george needs the mean function to be in this format
        class snModel(Model):
            def get_value(self, param):
                t = (param[:,0] * 1./t_stretch) + t_shift
                wv = param[:,1]
                return np.asarray([template(*p)[0] for p in zip(t,wv)]) + f_stretch

        #Get Test data to plot
        mean = snModel()

        for i in ufilts_in_angstrom:
            test_wv = np.full((1,round(np.max(times))-round(np.min(times))),i)
            test_times = np.arange(round(np.min(times)),round(np.max(times)))
            test_x = np.vstack((test_times,test_wv)).T
            test_y.append(mean.get_value(test_x))
        test_y=np.asarray(test_y)

    #set up gp
    kernel = np.var(lc[:,1]) * george.kernels.ExpSquaredKernel([50, 0.5], ndim=2)
    if(use_mean == False):
        gp = george.GP(kernel, mean = 0)
    else:
        gp = george.GP(kernel, mean = snModel())
    gp.compute(stacked_data, lc[:,-2])

    def neg_ln_like(p):
        gp.set_parameter_vector(p)
        return -gp.log_likelihood(lc[:,1])

    def grad_neg_ln_like(p):
        gp.set_parameter_vector(p)
        return -gp.grad_log_likelihood(lc[:,1])

    #optomize gp parameters
    result = minimize(neg_ln_like,
                      gp.get_parameter_vector(),
                      jac=grad_neg_ln_like)
    gp.set_parameter_vector(result.x)

    #populate arrays with time and wavelength values to be fed into gp
    for jj, time in enumerate(lc[:,0]):
        x_pred[jj*nfilts:jj*nfilts+nfilts, 0] = [time]*nfilts
        x_pred[jj*nfilts:jj*nfilts+nfilts, 1] = ufilts

    pred, pred_var = gp.predict(lc[:,1], x_pred, return_var=True)

    #populate dense_lc with newly gp-predicted values
    for jj in np.arange(nfilts):
        gind = np.where(np.abs(x_pred[:, 1]-ufilts[jj])<epsilon)[0]
        dense_fluxes[:, int(jj)] = pred[gind]
        dense_errs[:, int(jj)] = np.sqrt(pred_var[gind])
    dense_lc = np.dstack((dense_fluxes, dense_errs))
    
    return dense_lc, test_y, test_times


def fit_bb(dense_lc,wvs):
    '''
    Fit a series of BBs to the GP LC
    Adapted from superbol, Nicholl, M. 2018, RNAAS)

    Parameters
    ----------
    dense_lc : numpy.array
        GP-interpolated LC
    wvs : numpy.array
        Reference wavelengths in Ang

    Output
    ------
    T_arr : numpy.array
        BB temperature array (K)
    R_arr : numpy.array
        BB radius array (cm)
    Terr_arr : numpy.array
        BB radius error array (K)
    Rerr_arr : numpy.array
        BB temperature error array (cm)
    '''

    T_arr = np.zeros(len(dense_lc))
    R_arr = np.zeros(len(dense_lc))
    Terr_arr = np.zeros(len(dense_lc))
    Rerr_arr = np.zeros(len(dense_lc))

    for i,datapoint in enumerate(dense_lc):

        fnu = 10.**((-datapoint[:,0] + 48.6)/-2.5)
        ferr = datapoint[:,1]
        fnu = fnu * 4. * np.pi * (3.086e19)**2 #LAZZYYYY assumption of 10 kpc
        fnu_err = np.abs(0.921034 * 10.**(0.4 * datapoint[:,0] - 19.44)) * \
                ferr * 4. * np.pi * (3.086e19)**2

        flam = fnu * c / (wvs * ang_to_cm)**2 
        flam_err = fnu_err * c / (wvs * ang_to_cm)**2

        try:
            BBparams, covar = curve_fit(bbody,wvs,flam,
                                p0=(9000,1e15),sigma=flam_err)
            # Get temperature and radius, with errors, from fit
            T1 = BBparams[0]
            T1_err = np.sqrt(np.diag(covar))[0]
            R1 = np.abs(BBparams[1])
            R1_err = np.sqrt(np.diag(covar))[1]
        except:
            T1 = np.nan
            R1 = np.nan
            T1_err = np.nan
            R1_err = np.nan

        T_arr[i] = T1
        R_arr[i] = R1
        Terr_arr[i] = T1_err
        Rerr_arr[i] = R1_err

    return T_arr,R_arr,Terr_arr,Rerr_arr


def plot_gp(lc, dense_lc, snname, flux_corr, my_filters, wvs, test_data, outdir, sn_type, test_times, mean, show_template):
    '''
    Plot the GP-interpolate LC and save

    Parameters
    ----------
    lc : numpy.array
        Original LC data
    dense_lc : numpy.array
        GP-interpolated LC
    snname : string
        SN Name
    flux_corr : float
        Flux correction factor for GP
    my_filters : list
        List of filters
    wvs : numpy.array
        List of central wavelengths, for colors
    outdir : string
        Output directory

    Output
    ------
    '''

    cm = plt.get_cmap('rainbow') 
    wv_colors = (wvs - np.min(wvs)) / (np.max(wvs) - np.min(wvs))

    gind = np.argsort(lc[:,0])
    for jj in np.arange(len(wv_colors)):
        plt.plot(lc[gind,0],-dense_lc[gind,jj,0],color=cm(wv_colors[jj]),
                label=my_filters[jj].split('/')[-1])
        if(mean == True):
            if(show_template == True):
                plt.plot(test_times,-(test_data[jj,:] + flux_corr),'--',color=cm(wv_colors[jj]))#template curves
        plt.fill_between(lc[gind,0],-dense_lc[gind,jj,0]-dense_lc[gind,jj,1],
                    -dense_lc[gind,jj,0]+dense_lc[gind,jj,1],
                    color=cm(wv_colors[jj]),alpha=0.2)

    for i,filt in enumerate(np.unique(lc[:,2])):
        gind = np.where(lc[:,2]==filt)
        plt.plot(lc[gind,0],-(lc[gind,1] + flux_corr),'o',
                color=cm(wv_colors[i]))
    if(mean == True):
        plt.title(snname + ' using sn' + sn_type + ' template')
    else:
        plt.title(snname)
    plt.legend()
    plt.xlabel('Time(days)')
    plt.ylabel('Absolute Magnitudes')
    # Uhg, magnitudes are the worst.
    plt.gca().invert_yaxis()
    plt.savefig(outdir+snname+'_gp.png')
    plt.clf()

    return 1


def plot_bb_ev(lc, Tarr, Rarr, Terr_arr, Rerr_arr, snname, outdir):
    '''
    Plot the BB temperature and radius as a function of time

    Parameters
    ----------
    lc : numpy.array
        Original LC data
    T_arr : numpy.array
        BB temperature array (K)
    R_arr : numpy.array
        BB radius array (cm)
    Terr_arr : numpy.array
        BB radius error array (K)
    Rerr_arr : numpy.array
        BB temperature error array (cm)
    snname : string
        SN Name
    outdir : string
        Output directory

    Output
    ------
    '''

    fig,axarr = plt.subplots(2,1,sharex=True)
    axarr[0].plot(lc[:,0],Tarr/1.e3,'ko')
    axarr[0].errorbar(lc[:,0],Tarr/1.e3,yerr=Terr_arr/1.e3,fmt='none',color='k')
    axarr[0].set_ylabel('Temp. (1000 K)')

    axarr[1].plot(lc[:,0],Rarr/1e15,'ko')
    axarr[1].errorbar(lc[:,0],Rarr/1e15,yerr=Rerr_arr/1e15,fmt='none',color='k')
    axarr[1].set_ylabel(r'Radius ($10^{15}$ cm)')

    axarr[1].set_xlabel('Time (Days)')
    axarr[0].set_title(snname)

    plt.savefig(outdir+snname+'_bb_ev.png')
    plt.clf()

    return 1


def plot_bb_bol(lc, bol_lum, bol_err, snname, outdir):
    '''
    Plot the BB bolometric luminosity as a function of time

    Parameters
    ----------
    lc : numpy.array
        Original LC data
    bol_lum : numpy.array
        BB bolometric luminosity (erg/s)
    bol_err : numpy.array
        BB bolometric luminosity error (erg/s)
    snname : string
        SN Name
    outdir : string
        Output directory

    Output
    ------
    '''

    plt.plot(lc[:,0],bol_lum,'ko')
    plt.errorbar(lc[:,0],bol_lum,yerr=bol_err,fmt='none',color='k')

    plt.title(snname)
    plt.xlabel('Time (Days)')
    plt.ylabel('Bolometric Luminosity')
    plt.yscale('log')
    plt.savefig(outdir+snname+'_bb_bol.png')
    plt.clf()

    return 1


def write_output(lc, dense_lc,Tarr,Terr_arr,Rarr,Rerr_arr,
                 bol_lum,bol_err,my_filters,
                 snname,outdir):
    '''
    Write out the interpolated LC and BB information

    Parameters
    ----------
    lc : numpy.array
        Initial light curve
    dense_lc : numpy.array
        GP-interpolated LC
    T_arr : numpy.array
        BB temperature array (K)
    Terr_arr : numpy.array
        BB radius error array (K)
    R_arr : numpy.array
        BB radius array (cm)
    Rerr_arr : numpy.array
        BB temperature error array (cm)
    bol_lum : numpy.array
        BB luminosity (erg/s)
    bol_err : numpy.array
        BB luminosity error (erg/s)
    my_filters : list
        List of filter names
    snname : string
        SN Name
    outdir : string
        Output directory

    Output
    ------
    '''

    times = lc[:,0]
    dense_lc = np.reshape(dense_lc,(len(dense_lc),-1))
    dense_lc = np.hstack((np.reshape(-times,(len(times),1)),dense_lc))
    tabledata = np.stack((Tarr/1e3,Terr_arr/1e3,Rarr/1e15,
                        Rerr_arr/1e15, np.log10(bol_lum), np.log10(bol_err))).T
    tabledata = np.hstack((-dense_lc,tabledata)).T

    ufilts = np.unique(my_filters)
    table_header = []
    table_header.append('Time (MJD)')
    for filt in ufilts:
        table_header.append(filt)
        table_header.append(filt+'_err')
    table_header.extend(['Temp./1e3 (K)','Temp. Err.',
        'Radius/1e15 (cm)','Radius Err.', 'Log10(Bol. Lum)', 'Log10(Bol. Err)'])
    table = Table([*tabledata],
        names = table_header,
                meta={'name': 'first table'})

    format_dict = {head:'%0.3f' for head in table_header}
    ascii.write(table, outdir+snname+'.txt', formats=format_dict, overwrite=True)
    
    return 1


def main(snfile, dm=38.38):

    parser = argparse.ArgumentParser(description='extrabol helpers')
    parser.add_argument('snfile', nargs='?', default='./extrabol/example/Gaia16apd.dat',
                    type=str, help='Give name of SN file')
    parser.add_argument('-m', '--mean', dest='mean', type=str, default='0', 
                    help="Template function for gp. Choose \'1a\',\'1bc\', \'2l\', \'2p\', or \'0\' for no template" )
    parser.add_argument('-t', '--show_template', dest='template',
                    action='store_true', help="Shows template function on plots")
    parser.add_argument('-d','--dist', dest='distance', type=float,
                    help='Object luminosity distance', default=1e-5)
    parser.add_argument('-z','--redshift', dest='redshift', type=float,
                    help='Object redshift', default = 1.)   #redshift can't =1, this is simply a flag to be replaced later
    parser.add_argument('--dm', dest='dm', type=float, default=0,
                    help='Object distance modulus')
    parser.add_argument("--verbose", help="increase output verbosity",
                    action="store_true")
    parser.add_argument("--plot", help="Make plots",dest='plot',
                    type=bool, default=True)
    parser.add_argument("--outdir", help="Output directory",dest='outdir',
                    type=str, default='./products/')
    parser.add_argument("--ebv", help="MWebv",dest='ebv',
                    type=float, default=1.) #ebv won't =1, this is another flag to be replaced later
    parser.add_argument("--hostebv", help="Host B-V",dest='hostebv',
                    type=float, default=0.0)
    parser.add_argument('-s', '--start', help = 'The time of the earliest data point to be accepted',
                    type = float, default = 0)
    parser.add_argument('-e', '--end', help = 'The time of the latest data point to be accepted',
                    type = float, default = 200)
    parser.add_argument('-snr',  help = 'The minimum signal to noise ratio to be accepted',
                    type = float, default = 4)
    
    args = parser.parse_args()

    sn_type = args.mean
    try:
        sn_type=int(sn_type)
        mean = False
    except ValueError:
        sn_type = sn_type
        mean = True

    if args.redshift == 1 or args.ebv == 1:
        f = open(args.snfile, 'r')             #read in redshift and ebv and replace values if not specified
        if args.redshift == 1:
            args.redshift = float(f.readline())
            if args.ebv == 1:
                args.ebv = float(f.readline())
        if args.ebv == 1:
            args.ebv = float(f.readline())
            args.ebv = float(f.readline())
        f.close

    if (args.redshift != 0) | (args.distance != 1e-5) | (args.dm != 0):
        if args.redshift !=0 :
            args.distance = cosmo.luminosity_distance(args.redshift).value
            args.dm = cosmo.distmod(args.redshift).value
        elif args.distance != 1e-5:
            args.redshift = z_at_value(cosmo.luminosity_distance,distance * u.Mpc)
            dm = cosmo.distmod(args.redshift).value
        else:
            args.redshift = z_at_value(cosmo.distmod,dm * u.mag)
            distance = cosmo.luminosity_distance(args.redshift).value
    elif args.verbose:
        print('Assuming absolute magnitudes.')

    if args.outdir[-1] != '/':
        args.outdir+='/'

    if not os.path.exists(args.outdir):
        os.makedirs(args.outdir)

    snname = ('.').join(args.snfile.split('.')[:-1]).split('/')[-1]

    lc,wv_corr,flux_corr, my_filters = read_in_photometry(args.snfile, args.dm, args.redshift, args.start, args.end, args.snr)

    if sn_type == 'test':
        sn_type = test(lc, wv_corr, args.redshift)
    print(sn_type)

    dense_lc, test_data, test_times = interpolate(lc, wv_corr, sn_type, mean, args.redshift)
    lc = lc.T

    wvs,wvind = np.unique(lc[:,2],return_index=True)
    wvs = wvs * 1000.0 + wv_corr
    my_filters = np.asarray(my_filters)
    ufilts = my_filters[wvind]


    dense_lc[:,:,0] += flux_corr # This is now in AB mags

    Tarr,Rarr,Terr_arr,Rerr_arr = fit_bb(dense_lc,wvs)

    bol_lum = 4. * np.pi * Rarr **2 * sigsb * Tarr**4
    bol_err = 4. * np.pi * sigsb * np.sqrt(\
                (2. * Rarr * Tarr**4 * Rerr_arr)**2 + \
                (4. * Tarr**3 * Rarr**2 * Terr_arr)**2)

    if args.plot:
        if args.verbose:
            print('Making plots in '+args.outdir)
        plot_gp(lc,dense_lc,snname,flux_corr,ufilts,wvs,test_data,args.outdir, sn_type, test_times, mean, args.template)
        plot_bb_ev(lc,Tarr,Rarr,Terr_arr,Rerr_arr,snname,args.outdir)
        plot_bb_bol(lc, bol_lum, bol_err, snname, args.outdir)

    if args.verbose:
        print('Writing output to '+args.outdir)
    write_output(lc,dense_lc,Tarr,Terr_arr,Rarr,Rerr_arr,bol_lum,bol_err,my_filters,
                snname,args.outdir)


if __name__ == "__main__":
    main('./extrabol/example/Gaia16apd.dat',  dm = 38.38)
