"""Visibility figure presentation with explicit estimators and units."""
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

from .science_array import components


def draw_visibility_views(req, z, stamps, freq, labels):
    import matplotlib.pyplot as plt
    from matplotlib.colors import Normalize
    from matplotlib.ticker import FuncFormatter
    from casm_vis_analysis.plotting import format_time_range
    from .science import _gap_phase
    if z.shape != (len(stamps),len(labels),len(freq)):
        raise ValueError('Visibility time/baseline/frequency axes disagree')
    quantity = {'amplitude':'amp','autos':'amp'}.get(req.kind.split('_')[0],req.kind.split('_')[0])
    names = dict(amp='|V|',real='Real(V)',imag='Imag(V)',phase='Phase')
    reference = 'Raw' if req.reference == 'raw' else 'Sun fringe-stopped'
    unit = 'rad' if quantity=='phase' else 'counts'
    figs=[]
    if req.kind == 'amplitude_waterfall' and req.amplitude_normalization == 'channel_mean':
        from casm_vis_analysis.solar_waterfall import plot_dynamic_spectrum
        for k,label in enumerate(labels):
            fig=plot_dynamic_spectrum(np.abs(z[:,k]),stamps,freq,title=label+' · '+reference,
                                      tz=req.time_tz,quantity='|V|',integration_s=137.438953472)
            # The shared solar style uses a black mean curve on light figures.
            if plt.rcParams['axes.facecolor'] == 'black':
                fig.axes[2].lines[-1].set_color('white')
            figs.append(fig)
        return figs
    if req.kind.endswith('waterfall'):
        plotted,times=_gap_phase(z,stamps)
        values=components(plotted)[quantity]
        hours=(times-stamps[0])/3600
        for k,label in enumerate(labels):
            a=values[:,k];finite=a[np.isfinite(a)]
            if quantity=='phase':
                lo,hi,cmap=-np.pi,np.pi,'twilight_shifted'
            elif quantity in ('real','imag'):
                limit=max(float(np.percentile(np.abs(finite),98)) if len(finite) else 1.,1e-12)
                lo,hi,cmap=-limit,limit,'RdBu_r'
            else:
                lo,hi,cmap=0,max(float(np.percentile(finite,98)) if len(finite) else 1.,1e-12),'magma'
            palette=plt.get_cmap(cmap).copy();palette.set_bad('#1d232b')
            fig,ax=plt.subplots(figsize=(12,3.5))
            mesh=ax.pcolormesh(hours,freq,a.T,cmap=palette,norm=Normalize(lo,hi),shading='auto',rasterized=True)
            ax.set_title(label+' · '+reference,loc='left',fontsize=11,pad=12)
            ax.set_ylabel('Frequency (MHz)',fontsize=10)
            ax.xaxis.set_major_formatter(FuncFormatter(lambda h,pos:datetime.fromtimestamp(float(stamps[0])+h*3600,ZoneInfo(req.time_tz)).strftime('%m-%d\n%H:%M')))
            ax.set_xlabel('UTC' if req.time_tz=='UTC' else 'OVRO local (PDT/PST)',fontsize=10)
            ax.tick_params(labelsize=9)
            cb=fig.colorbar(mesh,ax=ax,label=f'{names[quantity]} ({unit})',pad=.02,fraction=.025)
            if quantity=='phase': cb.set_ticks([-np.pi,0,np.pi],labels=['−π','0','π'])
            fig.text(.075,.985,format_time_range(stamps,req.time_tz),ha='left',va='top',fontsize=9,color='#b6c2ce')
            fig.subplots_adjust(left=.075,right=.94,bottom=.18,top=.81)
            figs.append(fig)
    else:
        latest=req.spectrum_statistic=='latest'
        values=components(z[-1] if latest else z,axis=None if latest else 0)[quantity]
        ncols=min(3,len(labels));nrows=int(np.ceil(len(labels)/ncols))
        fig,axes=plt.subplots(nrows,ncols,squeeze=False,figsize=(4*ncols,2.8*nrows+.5),sharex=True)
        for k,label in enumerate(labels):
            ax=axes.flat[k]
            ax.plot(freq,values[k],linewidth=.65,marker='.' if quantity=='phase' else None,
                    linestyle='None' if quantity=='phase' else '-',markersize=1.8)
            ax.set_title(label,loc='left',fontsize=9)
            ax.set_xlabel('Frequency (MHz)')
            ax.set_ylabel(f'{names[quantity]} ({unit})')
            ax.grid(alpha=.15)
            if quantity=='phase':ax.set_ylim(-np.pi,np.pi)
        for ax in list(axes.flat)[len(labels):]:ax.set_visible(False)
        summary='Latest integration' if latest else ('Window mean |V|' if quantity=='amp' else 'Window complex mean')
        shown=stamps[-1:] if latest else stamps
        fig.suptitle(reference+' · '+summary+'\n'+format_time_range(shown,req.time_tz),fontsize=10)
        fig.tight_layout(rect=[0,0,1,.9])
        figs.append(fig)
    return figs
