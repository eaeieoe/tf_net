from train_leaderboard_final import to_test, train_test # Specifiegs training, leaderboard and final cell types
import sys
import numpy as np

import sklearn.metrics # For AUC and auPR

from scipy.special import expit

from itertools import izip

import timeit
import os

import tf_net

import utils

# Used to load one_hot module which relies on cython
import pyximport

# you may not need this, I do on some systems and not others
pyximport.install(setup_args={"include_dirs":np.get_include() } )

import one_hot 

import theano

floatX=theano.config.floatX

import random

from collections import OrderedDict

import read_cuts

# Check if variable exists (useful for interactive testing)
def var_exists(a):
    return( a in vars() or a in globals() )

DATADIR=os.environ["DREAM_ENCODE_DATADIR"]

# Net hyparameters
seq_flank=200
sequence_length=600

num_conv_layers=3
num_dense_layers=2
filter_widths=(10,10,10)
pool_sizes=(4,4,4)

# Check these are valid
assert( tf_net.check_valid(sequence_length, filter_widths, pool_sizes) )

n_channels=np.ones( num_conv_layers, dtype=int )*30

l2reg=1e-6
n_hidden=20

# What needs loading? Useful for running interactively.
print("Loading genome")
if not var_exists('dicgen'):
    dicgen = utils.get_fasta_chrom(DATADIR + "hg19.genome.fa.gz", [ "chr%i" % i for i range(1,23) ] )
    
batch_size=1000

import gzip

try:
    tf=to_test.keys()[ int(os.environ["SLURM_ARRAY_TASK_ID"]) ]
except:
    tf="ARID3A" # For testing

#tf=[ "CEBPB", "CTCF", "MAX" ][ int(os.environ["SLURM_ARRAY_TASK_ID"]) ]

print("TF: " + tf)

label_filename=DATADIR+"/labels/%s.train.labels.tsv.gz" % tf

# Mapping of labels to oridinal scale
label_dict={ "U":0.0, "A":1.0, "B":2.0 }

# Just want to get the training cell types
for l in gzip.open(label_filename,"rb"):
    lsplit=l.strip().split("\t")
    cell_types=lsplit[3:]
    break

# GE PCs generated by gene_expression_pca.R
if len(cell_types)>1:
    ge=np.genfromtxt("ge_pca.txt", names=True, dtype=floatX, deletechars="")

# Note we're not allowed to use conservation for the challenge
use_cons=False
if use_cons:
    import pyBigWig
    bw = pyBigWig.open(DATADIR+"/hg19.100way.phyloP100way.bw")
    
print("Compiling theano functions...")
n_hidden_array=np.ones(num_dense_layers,dtype=int)*n_hidden

# Whether to use the network which simultaneously considers the reverse complement
use_double_net=True
if use_double_net:
    import double_net
    rotation=(3,2,1,0) # order is ACGT
    rotation += (5,4) # flip + and - DGF
    if use_cons==3: rotation += 6 # conservation stays in same place
    train_func,test_func,pred_func,params=double_net.net(sequence_length, ge.shape[0] if (len(cell_types)>1) else 0, n_channels, filter_widths, pool_sizes, n_hidden_array, rotation=rotation, l2reg=l2reg, additional_channels=3 if (use_cons) else 2 )
else:
    train_func,test_func,pred_func,params=tf_net.net(sequence_length, ge.shape[0] if (len(cell_types)>1) else 0, n_channels, filter_widths, pool_sizes, n_hidden_array, l2reg=l2reg, additional_channels=3 if (use_cons) else 2 )

dgf_lookup={}
for cell_type in cell_types:
    print("Loading DGF for " + cell_type)
    dgf_lookup[cell_type]=read_cuts.read_both_strands_corrected(cell_type)

# Run a train (or test) epoch
def run_me(which_cell_types,train_test_func,unbound_rate=0.01):
    neglikes=[]
    first_line=True

    # As we go through the label file we iteratively build up:
    seq=[] # Training sequences
    y=[] # Labels (U/A/B)
    cts=[] # Cell types
    other_per_base=[] # Other per-base covariates, in this case the DNase cuts on the +/- strands
    
    batch_counter=0L

    labs=[]
    preds=[]

    # Iterate through the label_file
    label_file=gzip.open(label_filename,"rb")
    for l in label_file:
        lsplit=l.strip().split("\t")
        if first_line:
            cell_types=lsplit[3:]
            first_line=False
            continue
        chrom=lsplit[0]
        start=int(lsplit[1])
        stop=int(lsplit[2])
        labels= lsplit[3:] 

        for lab_index in range(len(cell_types)):
            lab=labels[lab_index]
            # Skip a lot of U during training
            if lab=="U" and np.random.rand() > unbound_rate: 
                continue
            cell_type=cell_types[lab_index]
            if not cell_type in which_cell_types: continue
            context=(start-seq_flank, stop+seq_flank)
            s=utils.fetch_sequence(dicgen, chrom, context[0], context[1], "+")
            if len(s)<sequence_length: continue
            seq.append(s)

            y.append( lab )
            context=np.array( context, dtype=np.uint32 )
            to_stack=[ dgf_lookup[cell_type](chrom,strand,context[0],context[1]) for strand in ("+","-") ]
            
            if use_cons:
                cons=bw.values(chrom, int(context[0]), int(context[1]))
                cons=np.array(cons,dtype=floatX)
                cons[ np.isnan(cons) ]=0.0
                to_stack.append( cons ) 
            other_per_base.append( np.vstack( to_stack ).transpose() )
            cts.append(cell_type)

        # Once we've loaded batch_size training samples...
        if len(seq) > batch_size:
            # Note one_hot_mat_N encodes N as [0,0,0,0]
            x_conv=np.dstack( [ np.concatenate( (one_hot.one_hot_mat_N( s ),o), axis=1 ) for s,o in izip(seq,other_per_base) ] )
            x_conv=utils.moveaxis(x_conv[:,:,:,np.newaxis], range(4), (3,1,0,2)) # workaround in case of not having numpy 1.11

            # Gene expression PCs
            x_flat=np.vstack( [ ge[cell_type] for cell_type in cts ] ) if (len(cell_types)>1) else np.zeros( ( len(seq), 0 ), dtype=theano.config.floatX )

            # Make the labels matrix
            to_stack=[]
            for lab in y:
                temp=np.zeros( 3, dtype=floatX)
                temp[ label_dict[lab] ]=(1.0/unbound_rate) if lab=="U" else 1.0 # compensate for U subsampling
                to_stack.append( temp )
            y_mat=np.vstack( to_stack )

            # Run training
            [p,neglike]=train_test_func( x_conv, x_flat, y_mat )
            neglikes.append(float(neglike))

            # Record labels and predictions for non-A for AUC calculations at end of epoch
            for i in range(len(y)):
                if not y=="A":
                    labs.append( 1 if y[i]=="B" else 0 )
                    preds.append( expit(p[i]) )

            # Clear the minibatch data
            seq=[]
            cts=[]
            other_per_base=[]
            y=[]

            batch_counter +=1
            if (batch_counter % 1000 == 0): print("%d %f" % (batch_counter, np.mean(neglikes)))
    label_file.close()
    return(np.array( (np.mean(neglikes), sklearn.metrics.roc_auc_score( labs, preds ), sklearn.metrics.average_precision_score( labs, preds ) ) ) )

#train_ct=cell_types[:-1]
train_ct=cell_types
#test_ct=cell_types[-1]
test_ct=[]

fits_dir=DATADIR+"fits/"
if not os.path.isdir(fits_dir): os.mkdir(fits_dir)

for epoch in range(20):
    print("Training")
    np.random.seed(epoch)
    train_metrics=run_me(train_ct, train_func)
    if len(test_ct)>0:
        print("Testing")
        np.random.seed(0) # fixed so always using the same negative set here
        test_metrics=run_me(test_ct, test_func)
    else:
        test_metrics=np.zeros( 0 )
    d={"epoch":epoch, "train_metrics": train_metrics, "test_metrics": test_metrics }

    # Save model and predictions to file
    for k,v in params.iteritems():
        d[k]=v.get_value() 
    np.savez(fits_dir+("%s.npz" % tf), **d)

    print("Epoch %d: train %s test %s" % (epoch+1, np.array_str(train_metrics, precision=2), np.array_str(test_metrics, precision=2) ) ) 


############ Output submissions #############
print("Model trained! Now testing...")

test_pairs=[ ("F",cell_type) for cell_type in train_test[tf]["final"] ] + [ ("L",cell_type) for cell_type in train_test[tf]["leaderboard"] ]

submissions_dir=DATADIR+"submissions/"
if not os.path.isdir(submissions_dir): os.mkdir(submissions_dir)

for (submission_type,cell_type) in test_pairs:
    print(submission_type + ":" + cell_type)
    dgf_lookup=read_cuts.read_both_strands_corrected(cell_type)
    outfile=gzip.open(submissions_dir+("%s.%s.%s.tab.gz" % (submission_type, tf,cell_type)), "wb")
    neglikes=[]
    seq=[]
    lines=[]
    other_per_base=[]

    def write_pred():
        x_conv=np.dstack( [ np.concatenate( (one_hot.one_hot_mat_N( s ),o), axis=1 ) for s,o in izip(seq,other_per_base) ] )
        x_conv=utils.moveaxis(x_conv[:,:,:,np.newaxis], range(4), (3,1,0,2)) # workaround until we have numpy 1.11

        x_flat=np.vstack( [ ge[cell_type] for dummy in seq ] ) if (len(cell_types)>1) else np.zeros( ( len(seq), 0 ), dtype=theano.config.floatX )

        logit_p=pred_func( x_conv, x_flat )

        prob=expit( logit_p - 1.0 )

        for i in range(len(seq)):
            outfile.write("%s\t%f\n" % (lines[i],prob[i]))

    
    for l in gzip.open(DATADIR+("%s_regions.blacklistfiltered.bed.gz" % ("ladder" if (submission_type=="L") else "test")),"rb"):
        l=l.strip()
        lsplit=l.split("\t")

        chrom=lsplit[0]
        start=int(lsplit[1])
        stop=int(lsplit[2])

        lines.append(l)
        
        context=(start-seq_flank, stop+seq_flank)
        s=utils.fetch_sequence(dicgen, chrom, context[0], context[1], "+")

        seq.append(s)

        context=np.array( context, dtype=np.uint32 )
        other_per_base.append( np.vstack( [ dgf_lookup(chrom,strand,context[0],context[1]) for strand in ("+","-") ] ).transpose() )
            
        if len(seq) > batch_size:
            write_pred()
            
            lines=[]
            seq=[]
            other_per_base=[]

    write_pred()
    
    outfile.close()
    

