
from __future__ import print_function, division

import os, h5py, numpy as np
import fast_histogram
import logging
    
"""
TO DO: 

- debug / convert this to work on f2scan and/or fscan2d data
- treat 360 scans starting near the center (monitor/hits)
- send pyFAI style integration jobs
- mca harvesting for fluo tomo
- scalar reconstructions (counters, roi, etc)
- 1d reconstructions (fluoct xrdct)
- run peaksearch/segmentations
- peak sinograms
- linking or merging peaks across dty/omega
- indexing and reconstructing grains

"""
    
def guess_chunks(name, shape):
    if name == 'omega':
        return ( shape[0], 1 )
    if name == 'dty':
        return ( 1, shape[1] )
    return shape
        
        
class DataSet:
    
    # simple strings or ints
    ATTRNAMES = ( "dataroot", "analysisroot", "sample", "dset", "shape", "dsname", 
                  "datapath", "analysispath", "masterfile", 
                  "limapath"
                )
    STRINGLISTS = ( "scans", "imagefiles", "sparsefiles" )
    # sinograms
    NDNAMES = ( "omega", "dty", "nnz", "frames_per_file" )

    def __init__(self,
                 dataroot = ".",
                 analysisroot = ".",
                 sample = "sample", 
                 dset = "dataset"):
        """ The things we need to know to process data """

        self.detector = 'eiger' # frelon3
        self.limapath = None
    
        self.omegamotor = 'rot_center'      # diffrz
        self.dtymotor   = 'dty'      # diffty

        self.dataroot = dataroot
        self.analysisroot = analysisroot
        self.sample = sample
        self.dset = dset
        
        self.dsname = "_".join( (self.sample, self.dset ) )

        self.datapath = os.path.join( self.dataroot,  self.sample, self.dsname )
        self.analysispath = os.path.join( self.analysisroot,  self.sample, self.dsname )
        self.masterfile = os.path.join( self.datapath, self.dsname + '.h5' )
        
        # These are in order !
        self.scans = None # read from master or load from analysis        
        self.imagefiles = []        # List of strings. w.r.t self.datapath
        self.frames_per_file = []    # how many frames in this file
        self.sparsefiles = []       # matches to self.imagefiles
        
        self.shape = (0, 0)
        self.omega = None
        self.dty = None
                
        
    def __repr__(self):
        r = []
        for name in "dataroot analysisroot sample dset".split(): 
            r.append('%s = "%s"'%( name, getattr(self, name) ) )
        r.append( 'shape = ( %d, %d)'%tuple(self.shape) )
        if self.scans is not None:
            r.append( '# scans %d from %d to %d'%(
                len(self.scans), self.scans[0], self.scans[-1] ))
        return  "\n".join(r)

    
    def compare(self, other):
        '''Try to see if the load/save is working'''
        from types import FunctionType
        sattrs = set( [ name for name in vars(self) if name[0] != '_' ] )
        oattrs = set( [ name for name in vars(self) if name[0] != '_' ] )
        if sattrs != oattrs:
            logging.info('Attribute mismatch '+str(sattrs)+' != '+str(oattrs))
            return False
        for a in sattrs:
            s = getattr( self, a )
            if isinstance(s, FunctionType):
                continue
            o = getattr( other, a )
            t = type(s)
            if type(o) != type(s):
                logging.info('Type mismatch %s %s'%(str(t),str(a)))
                return False
            if t == np.ndarray:
                if s.shape != o.shape:
                    logging.info('Shape mismatch %s %s'%(str(s.shape), str(o.shape)))
                    return False
                if ( s != o ).all():
                    logging.info('Data mismatch '+str(a))
                    return False
            else:
                if s != o:
                    logging.info('Data mismatch ')
                    return False
        logging.info('Dataset objects seem to match!')
        return True
                
        
        
    def report(self):
        print(self)
        print("# Collected %d missing %d"%(self.check_images()))
        print("# Segmented %d missing %d"%(self.check_sparse()))

    
    def import_all(self):
        # collect the data
        self.import_scans()
        # lima frames
        self.import_imagefiles()
        # motor positions
        self.import_motors_from_master()
        # pixels per frame
        try:
            self.import_nnz()
        except:
            logging.info("nnz not available. Segmentation done?")
            
        
    def import_scans(self, hname = None):
        """ Reads in the scans from the bliss master file """
        if hname is None:
            hname = self.masterfile
        with h5py.File( hname, 'r' ) as hin:
            scans = list( hin['/'] )
        self.scans = [scan for scan in scans if scan.endswith('.1')]
        self.shape = (len(self.scans), self.shape[1])
        logging.info( 'imported %d scans from %s'%(len(self.scans),hname))
        return self.scans
    
    
    def import_imagefiles(self):
        """ Get the Lima file names from the bliss master file, also scan_npoints """
        npts = None
        self.imagefiles = []
        self.frames_per_file = []
        with h5py.File( self.masterfile, 'r' ) as hin:
            for i, scan in enumerate( self.scans ):
                if ('measurement' not in hin[scan]) or (self.detector not in hin[scan]['measurement']):
                    continue
                frames = hin[scan]['measurement'][self.detector]
                imageshape = frames.shape[1:]
                if npts is None:
                    npts = len(frames)
                else:
                    assert len(frames) == npts, 'scan is not regular %d %s :: %s'%(npts, self.masterfile, scan)
                for vsrc in frames.virtual_sources():
                    self.imagefiles.append( vsrc.file_name )
                    self.frames_per_file.append( vsrc.src_space.shape[0] ) # not sure about this
                    # check limapath
                    if self.limapath is None:
                        self.limapath = vsrc.dset_name
                    assert self.limapath == vsrc.dset_name
        self.frames_per_file = np.array( self.frames_per_file, int )
        self.sparsefiles = [ name.replace( '/', '_' ).replace( '.h5', '_sparse.h5' ) for name in 
                             self.imagefiles ]
        self.shape = (self.shape[0], npts)
        logging.info( 'imported %d lima filenames'%( np.sum(self.frames_per_file) ) )
        logging.info( 'sinogram shape = ( %d , %d ) imageshape = ( %d , %d)'%(
            self.shape[0], self.shape[1], imageshape[0], imageshape[1] ) )
                     
            
    def import_motors_from_master(self):  #  could also get these from sparse files if saved
        """ read the motors from the lima file
        you need to import the imagefiles first
        these will be the motor positions to accompany the images
        """
        self.omega = np.zeros( self.shape, float )  # list of counters for monitor?
        self.dty   = np.zeros( self.shape, float )
        with h5py.File( self.masterfile, 'r' ) as hin:
            for i, scan in enumerate( self.scans ):
                # Should always be there, if not, filter scans before you get to here
                self.omega[i] =  hin[scan][ 'measurement' ][ self.omegamotor ][()]
                self.dty[i]   =  hin[scan][ 'instrument/positioners' ][ self.dtymotor ][()]
        logging.info( 'imported omega/dty' )
        self.guessbins()

        
    def guessbins(self):
        ny, nomega = self.shape
        self.omin = self.omega.min()
        self.omax = self.omega.max()
        # values 0, 1, 2
        # shape = 3
        # step = 1
        self.ostep = (self.omax - self.omin) / (nomega - 1)
        self.ymin = self.dty.min()
        self.ymax = self.dty.max()
        self.ystep = (self.ymax - self.ymin) / (ny - 1)
        self.obincens = np.linspace( self.omin, self.omax, nomega )
        self.ybincens = np.linspace( self.ymin, self.ymax, ny )
        self.obinedges = np.linspace( self.omin-self.ostep/2, self.omax + self.ostep/2, nomega + 1 )
        self.ybinedges = np.linspace( self.ymin-self.ystep/2, self.ymax + self.ystep/2, ny + 1 )
        
        
    def sinohist(self, weights=None, method='fast'):
        """ Bin some data onto the sinogram histogram """
        bins = len(self.obincens), len(self.ybincens)
        rng  = ( (self.obinedges[0], self.obinedges[-1]),
                 (self.ybinedges[0], self.ybinedges[-1]) )
        if isinstance( weights, np.ndarray):
            wt = weights.ravel()
        else:
            wt = weights
        if method == 'numpy':
            ret = np.histogram2d( self.omega.ravel(), self.dty.ravel(), 
                                weights = wt, bins = bins, range=rng )
            histo = ret[0]
        elif method == 'fast':            
            histo = fast_histogram.histogram2d( self.omega.ravel(), self.dty.ravel(), 
                                 weights = wt, bins = bins, range=rng )
        return histo

    
    def import_nnz(self):
        """ Read the nnz arrays from the scans """
        nnz = []
        for spname in self.sparsefiles:
            with h5py.File( os.path.join( self.analysispath, spname ), "r" ) as hin:
                nnz.append( hin[self.limapath]['nnz'][:] )
        self.nnz = np.concatenate( nnz ).reshape( self.shape ).astype( np.int32 )
        logging.info('imported nnz, average %f'%(self.nnz.mean())) # expensive if you are not logging it.
  

    def check_files(self, path, filenames, verbose = 0):
        """ See whether files are created or not """
        # images collected
        done = 0
        missing = 0
        for fname in filenames:
            fullname = os.path.join( path, fname )
            if os.path.exists( fullname ):
                done += 1
            else:
                missing += 1
                if verbose>0: 
                    print("missing", fullname )
                    verbose -= 1
        return done, missing
    
    
    def check_images(self):
        """ Is the experiment finished ? """
        return self.check_files( self.datapath, self.imagefiles )
      
        
    def check_sparse(self):
        """ Has the segmentation been done ? """
        return self.check_files( self.analysispath, self.sparsefiles,  verbose=2 )
    
    
    def save( self, h5name, h5group = '/' ):
        
        ZIP = { 'compression': 'gzip' } 
        
        with h5py.File( h5name, "a") as hout:
            grp = hout[ h5group ]
            # Simple small objects
            for name in self.ATTRNAMES:
                data = getattr( self, name, None )
                if data is not None:
                    grp.attrs[ name ] = data 
            # The string lists
            for name in self.STRINGLISTS:
                data = getattr( self, name, None )
                if data is not None:
                    sdata = np.array( data, "S" )
                    grp.create_dataset( name, 
                                        data = sdata,
                                        chunks = sdata.shape,
                                        dtype = h5py.string_dtype(),
                                         **ZIP )
            #
            for name in self.NDNAMES:
                data = getattr(self, name, None)
                if data is not None:
                    chunks = guess_chunks(name, data.shape)
                    try:
                        grp.create_dataset( name, data=data,
                                        chunks = chunks,
                                        **ZIP )
                    except:
                        print(name, chunks, data.shape)
                        raise
            

    def load( self, h5name, h5group = '/' ):
        """ Recover this from a hdf5 file """
        with h5py.File( h5name, "r") as hin:
            grp = hin[ h5group ]
            for name in self.ATTRNAMES:
                if name in grp.attrs:
                    setattr( self, name, grp.attrs.get(name) )
            self.shape = tuple( self.shape ) # hum
            for name in self.NDNAMES:
                if name in grp:
                    data = grp[name][()]
                    setattr( self, name, data )      
            for name in self.STRINGLISTS:
                if name in grp:
                    data = list( grp[name][()] )
                    setattr( self, name, data )
        self.guessbins()
        return self


def np2str( inp ):
    return inp.tobytes().decode()


def load( h5name, h5group = '/' ):
    return DataSet().load( h5name, h5group )

# Example
#    s = dataset(
#        dataroot = "/data/visitor/ma5415/id11/20221027",
#        analysisroot = "/data/visitor/ma5415/id11/20221027/analysis/SparsePixels",
#        sample = "NSCOPE_SiMo1000_6",
#        dset = "DT600nm_Z1" )
#    s.import_all()

        
if __name__=="__main__":
    import sys
    logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)
    
    dataroot = sys.argv[1]
    analysisroot = sys.argv[2]
    sample = sys.argv[3]
    dset = sys.argv[4]
    
    h5o = DataSet( dataroot = dataroot, analysisroot = analysisroot, sample = sample, dset = dset )
    h5o.import_all()
    h5o.save( sys.argv[5] )
    
    print("Read back from hdf5")
    t = load( sys.argv[5] )
    t.report()
    t.compare(h5o)
    
