
from __future__ import print_function, division

""" Do segmentation of lima/eiger files with no notion of metadata
Blocking will come via lima saving, so about 1000 frames per file

Make the code parallel over lima files ... no interprocess communication intended
 - read an input file which has the source/destination file names for this job
"""


import sys, time, os, math, logging

from ImageD11.sinograms import dataset

# Code to clean the 2D image and reduce it to a sparse array:
# things we might edit
class SegmenterOptions:
    
    # These are the stuff that belong to us in the hdf5 file (in our group: lima_segmenter)
    jobnames = ( 'cut','howmany','pixels_in_spot',  
                 'maskfile', 'bgfile',
                 'cores_per_job', 'files_per_core')
                 
    # There are things that DO NOT belong to us
    datasetnames = ( 'limapath', 'analysispath', 'datapath', 'imagefiles', 'sparsefiles' )
    
    def __init__(self, 
                 cut = 1,          # keep values abuve cut in first look at image
                 howmany = 100000,      # max pixels per frame to keep
                 pixels_in_spot = 3,
                 maskfile = "",
                 bgfile = "",
                 cores_per_job = 8,
                 files_per_core = 8,
                 ):
        self.cut = cut
        self.howmany = howmany
        self.pixels_in_spot = pixels_in_spot
        self.maskfile = maskfile
        self.bgfile = bgfile
        self.files_per_core = files_per_core
        self.cores_per_job = cores_per_job
                      
    def __repr__(self):
        return "\n".join( ["%s:%s"%(name,getattr(self, name, None )) for name in self.jobnames + self.datasetnames ] )
                
    def setup(self):
        self.thresholds = tuple( [ self.cut*pow(2,i) for i in range(6) ] )
        # validate input
        if len(self.maskfile):
            self.mask = 1 - fabio.open(self.maskfile).data
            print("# Opened mask", self.maskfile)
        if len(self.bgfile):
            self.bg = fabio.open(self.bgfile).data
    
    def load(self, h5name, h5group):
        
        with h5py.File( h5name, "r" ) as hin:
            grp = hin[h5group]
            pgrp = grp.parent
            for name in self.jobnames:
                if name in grp.attrs:
                    setattr(self, name, grp.attrs.get( name ) )
            for name in self.datasetnames:
                if name in pgrp.attrs: 
                    setattr(self, name, pgrp.attrs.get( name ) )
                elif name in pgrp:
                    setattr(self, name, pgrp[name][()] )
                else:
                    logging.warning("Missing " + name )
        self.setup()
        
    def save(self, h5name, h5group):
        logging.info("saving to "+h5name+"::"+h5group)
        with h5py.File( h5name, "a" ) as hout:
            grp = hout.require_group( h5group )
            for name in self.jobnames:
                value = getattr( self, name, None )
                print(name, value)
                if value is not None:
                    grp.attrs[ name ] = value
                    
        
########################## should not need to change much below here


import functools
import numpy as np
import hdf5plugin
import h5py
import numba
import fabio
# pip install ImageD11 --no-deps # if you do not have it yet:
from ImageD11 import sparseframe, cImageD11


@numba.njit
def select(img, mask, row, col, val, cut):
    # Choose the pixels that are > cut and put into sparse arrays
    k = 0
    for s in range(img.shape[0]):
        for f in range(img.shape[1]):
            if img[s, f] > cut:
                if mask[s, f]:  # skip masked
                    continue
                row[k] = s
                col[k] = f
                val[k] = img[s, f]
                k += 1
    return k


@numba.njit
def top_pixels(nnz, row, col, val, howmany, thresholds):
    """
    selects the strongest pixels from a sparse collection
    - THRESHOLDS should be a sorted array of potential cutoff values to try
    that are higher than the original cutoff used to select data
    - howmany is the maximum number of pixels to return
    """
    # quick return if there are already few enough pixels
    if nnz <= howmany:
        return nnz
    # histogram of how many pixels are above each threshold
    h = np.zeros(len(thresholds), dtype=np.uint32)
    for k in range(nnz):
        for i, t in enumerate(thresholds):
            if val[k] > t:
                h[i] += 1
            else:
                break
    # choose the one to use. This is the first that is lower than howmany
    tcut = thresholds[-1]
    for n, t in zip(h, thresholds):
        if n < howmany:
            tcut = t
            break
    # now we filter the pixels
    n = 0
    for k in range(nnz):
        if val[k] > tcut:
            row[n] = row[k]
            col[n] = col[k]
            val[n] = val[k]
            n += 1
            if n >= howmany:
                break
    return n

OPTIONS = None  # global. Nasty.

def choose(frm):
    """ converts a frame to sparse frame"""
    if choose.cache is None:
        # cache the mallocs on this function. Should be one per process
        row = np.empty(OPTIONS.mask.size, np.uint16)
        col = np.empty(OPTIONS.mask.size, np.uint16)
        val = np.empty(OPTIONS.mask.size, frm.dtype)
        choose.cache = row, col, val
    else:
        row, col, val = choose.cache
    nnz = select(frm, OPTIONS.mask, row, col, val, OPTIONS.cut)
    if nnz == 0:
        sf = None
    else:
        if nnz > OPTIONS.howmany:
            nnz = top_pixels(nnz, row, col, val, OPTIONS.howmany,  OPTIONS.thresholds)
        # Now get rid of the single pixel 'peaks'
        #   (for the mallocs, data is copied here)
        s = sparseframe.sparse_frame(row[:nnz].copy(), col[:nnz].copy(), frm.shape)
        s.set_pixels("intensity", val[:nnz].copy())
        if OPTIONS.pixels_in_spot <= 1:
            sf = s
        else:
            # label them according to the connected objects
            sparseframe.sparse_connected_pixels(
                s, threshold=OPTIONS.cut, data_name="intensity", label_name="cp"
            )
            # only keep spots with more than 3 pixels ...
            mom = sparseframe.sparse_moments(
                s, intensity_name="intensity", labels_name="cp"
            )
            npx = mom[:, cImageD11.s2D_1]
            pxcounts = npx[s.pixels["cp"] - 1]
            pxmsk = pxcounts >= OPTIONS.pixels_in_spot
            if pxmsk.sum() == 0:
                sf = None
            else:
                sf = s.mask(pxmsk)
    return sf

choose.cache = None  # cache for malloc per process


def segment_lima( args ):
    """Does segmentation on a single hdf5
    srcname,
    destname,              
    dataset
    """
    srcname, destname, dataset = args
    # saving compression style:
    opts = {
        "chunks": (10000,),
        "maxshape": (None,),
        "compression": "lzf",
        "shuffle": True,
    }
    start = time.time()
    with h5py.File(destname, "a") as hout:
        with h5py.File(srcname, "r") as hin:
            if dataset not in hin:
                print("Missing",dataset,"in",srcname)
                return
            # TODO/fixme - copy some headers over
            print("# ", srcname, destname, dataset)
            print("# time now", time.ctime(), "\n#", end=" ")
            frms = hin[dataset]
            g = hout.require_group(dataset)
            row = g.create_dataset("row", (1,), dtype=np.uint16, **opts)
            col = g.create_dataset("col", (1,), dtype=np.uint16, **opts)
            # can go over 65535 frames in a scan
            # num = g.create_dataset("frame", (1,), dtype=np.uint32, **opts)
            sig = g.create_dataset("intensity", (1,), dtype=frms.dtype, **opts)
            nnz = g.create_dataset("nnz", (frms.shape[0],), dtype=np.uint32)
            g.attrs["itype"] = np.dtype(np.uint16).name
            g.attrs["nframes"] = frms.shape[0]
            g.attrs["shape0"] = frms.shape[1]
            g.attrs["shape1"] = frms.shape[2]
            npx = 0
            nframes = len(frms)
            for i,frame in enumerate(frms):
                spf = choose(frame)
                if i % 100 == 0:
                    if spf is None:
                        print("%4d 0" % (i), end=",")
                    else:
                        print("%4d %d" % (i, spf.nnz), end=",")
                    sys.stdout.flush()
                if spf is None:
                    nnz[i] = 0
                    continue
                if spf.nnz + npx > len(row):
                    row.resize(spf.nnz + npx, axis=0)
                    col.resize(spf.nnz + npx, axis=0)
                    sig.resize(spf.nnz + npx, axis=0)
                row[npx:] = spf.row[:]
                col[npx:] = spf.col[:]
                sig[npx:] = spf.pixels["intensity"]
                # num[npx:] = i
                nnz[i] = spf.nnz
                npx += spf.nnz
            g.attrs["npx"] = npx
    end = time.time()
    print("\n# Done", nframes,'frames',npx,'pixels','fps',nframes/(end-start) )
    return destname

    # the output file should be flushed and closed when this returns

OPTIONS = None

def main( options ):
    global OPTIONS
    OPTIONS = options
    args = []
    files_per_job = options.cores_per_job * options.files_per_core   # 64 files per job
    start = options.jobid*files_per_job
    end   = min( (options.jobid+1)*files_per_job, len(options.imagefiles) )
    for i in range(start, end):
        args.append( ( os.path.join( options.datapath, options.imagefiles[i] ), # src
                       os.path.join( options.analysispath, options.sparsefiles[i] ), # dest
                       options.limapath ) )
        
    import concurrent.futures
    with concurrent.futures.ProcessPoolExecutor(max_workers=options.cores_per_job) as mypool:
        donefile = sys.stdout
        for fname in mypool.map( segment_lima, args, chunksize=1 ):
            donefile.write(fname + "\n")
            donefile.flush()
    print("All done")
    
    
    
def setup_slurm_array( dsname, dsgroup='/', 
                       cores_per_job=8, 
                       files_per_core=8 ):
    """ Send the tasks to slurm """
    dso = dataset.load( dsname, dsgroup )
    nfiles = len(dso.sparsefiles)  
    dstlima = [ os.path.join( dso.analysispath, name ) for name in dso.sparsefiles ]
    done = 0
    for d in dstlima:
        if os.path.exists(d):
            done += 1
    print("total files to process", nfiles, 'done', done)
    if done == nfiles:
        return None
    sdir = os.path.join( dso.analysispath, 'slurm' )
    if not os.path.exists( sdir ):
        os.makedirs( sdir )
    options = SegmenterOptions( dsname, 'lima_segmenter' )
    files_per_job = options.files_per_core * options.cores_per_job
    jobs_needed = math.ceil( nfiles / files_per_job )
    sbat = os.path.join( sdir, "lima_segmenter_slurm.sh" )
    command = "python3 -m ImageD11.sinograms.lima_segmenter segment %s $SLURM_ARRAY_TASK_ID"%(dsname)
    with open(sbat ,"w") as fout:
        fout.write( """#!/bin/bash
#SBATCH --job-name=array-lima_segmenter
#SBATCH --output=%s/lima_segmenter_%A_%a.out
#SBATCH --error=%s/lima_segmenter_%A_%a.err
#SBATCH --array=0-%d
#SBATCH --time=02:00:00
# define memory needs and number of tasks for each array job
#SBATCH --ntasks=1
#SBATCH --cpus-per-task={options.cores_per_job}
#
date
echo Running on $HOSTNAME : %s
%s > %s/lima_segmenter_%A_%a_$SLURM_ARRAY_TASK_ID.log 2>&1
date
"""%(sdir,sdir,jobs_needed, command, command, sdir))
    logging.info("wrote "+sbat)
    return sbat


def setup():
    dsname = sys.argv[2]
    dso = dataset.load( dsname )
    options = SegmenterOptions()
    
    if 'eiger' in dso.limapath:
        options.cut = 1
        options.maskfile = "/data/id11/nanoscope/Eiger/mask_20210428.edf"
    elif 'frelon3' in dso.limapath:
        options.cut = 25,          # keep values abuve cut in first look at image
    else:
        print("I don't know what to do")
    options.save( dsname, 'lima_segmenter' )
    setup_slurm_array( dsname )

    
def segment():
    # we will use multiple processes, each with 1 core
    # all sub-processes better do this!
    os.environ["HDF5_USE_FILE_LOCKING"] = "FALSE"  # not sure we want/need this here?
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["NUMBA_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"

    # Uses a hdffile from dataset.py to steer the processing
    # everything is passing via this file. 
    # 
    h5name = sys.argv[2]
    
    # This assumes forking. To be investigated otherwise.
    options = SegmenterOptions()
    options.load( h5name, 'lima_segmenter' )   
    options.jobid = int( sys.argv[3] )
    main( options )

    
if __name__ == "__main__":
        
    if sys.argv[1] == 'setup':
        setup()
        
    if sys.argv[1] == 'segment':
        segment()
    