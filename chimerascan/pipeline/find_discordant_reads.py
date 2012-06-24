'''
Created on Jun 2, 2011

@author: mkiyer
'''
import logging
import collections
import os
import sys
import argparse

import pysam

from chimerascan.lib import config
from chimerascan.lib.base import LibraryTypes
from chimerascan.lib.sam import parse_pe_reads, pair_reads, copy_read, select_best_scoring_pairs
from chimerascan.lib.feature import TranscriptFeature
from chimerascan.lib.transcriptome_to_genome import build_tid_transcript_map, build_tid_transcript_genome_map, transcript_to_genome_pos
from chimerascan.lib.chimera import DiscordantTags, DISCORDANT_TAG_NAME, \
    ORIENTATION_TAG, ORIENTATION_5P, ORIENTATION_3P, get_orientation

def count_transcriptome_multimaps(bamfh, reads, tid_tx_genome_map):
    hits = set()
    for r in reads:
        if r.is_unmapped:
            return 0
        # TODO: remove assert statement
        assert r.tid in tid_tx_genome_map
        # use the position that is most 5' relative to genome
        left_tid, left_strand, left_pos = transcript_to_genome_pos(r.tid, r.pos, tid_tx_genome_map)
        right_tid, right_strand, right_pos = transcript_to_genome_pos(r.tid, r.aend-1, tid_tx_genome_map)
        hits.add((left_tid, left_pos, right_pos))
    return len(hits)

def map_reads_to_references(pe_reads, tid_tx_map):
    """
    bin reads by transcript cluster and reference (tid)
    """
    refdict = collections.defaultdict(lambda: ([], []))
    clusterdict = collections.defaultdict(lambda: ([], []))
    for readnum, reads in enumerate(pe_reads):
        for r in reads:
            if r.is_unmapped:
                continue 
            # TODO: remove assert statement
            assert r.tid in tid_tx_map
            # add to cluster dict
            cluster_id = tid_tx_map[r.tid].cluster_id
            pairs = clusterdict[cluster_id]
            pairs[readnum].append(r)
            # add to reference dict
            pairs = refdict[r.tid]
            pairs[readnum].append(r)
    return refdict, clusterdict

def classify_unpaired_reads(reads, library_type):
    gene_hits_5p = []
    gene_hits_3p = []
    for r in reads:
        # this alignment is to a transcript (gene), so need
        # to determine whether it is 5' or 3'
        orientation = get_orientation(r, library_type)
        if orientation == ORIENTATION_5P:
            gene_hits_5p.append(r)
        else:
            gene_hits_3p.append(r)
        # add a tag to the sam file describing the read orientation and
        # that it is discordant
        r.tags = r.tags + [(DISCORDANT_TAG_NAME, DiscordantTags.DISCORDANT_GENE),
                           (ORIENTATION_TAG, orientation)]                               
    return gene_hits_5p, gene_hits_3p

def find_discordant_pairs(pe_reads, library_type):
    """
    iterate through combinations of read1/read2 to predict valid 
    discordant read pairs
    """
    # classify the reads as 5' or 3' gene alignments or genome alignments
    r1_5p_gene_hits, r1_3p_gene_hits = \
        classify_unpaired_reads(pe_reads[0], library_type)
    r2_5p_gene_hits, r2_3p_gene_hits = \
        classify_unpaired_reads(pe_reads[1], library_type)
    # pair 5' and 3' gene alignments
    gene_pairs = []
    combos = [(r1_5p_gene_hits,r2_3p_gene_hits),
              (r1_3p_gene_hits,r2_5p_gene_hits)]
    for r1_list,r2_list in combos:
        for r1 in r1_list:
            for r2 in r2_list:
                cr1 = copy_read(r1)
                cr2 = copy_read(r2)
                pair_reads(cr1,cr2)
                gene_pairs.append((cr1,cr2))
    return gene_pairs

def classify_read_pairs(pe_reads, max_isize,
                        library_type, 
                        tid_tx_map):
    """
    examines all the alignments of a single fragment and tries to find ways
    to pair reads together.
    
    annotates all read pairs with an integer tag corresponding to a value
    in the DiscordantTags class
    
    returns a tuple with the following lists:
    1) pairs (r1,r2) aligning to genes (pairs may be discordant)
    3) unpaired reads, if any
    """
    # to satisfy library type reads must either be on 
    # same strand or opposite strands
    concordant_tx_pairs = []
    discordant_tx_pairs = []
    concordant_cluster_pairs = []
    discordant_cluster_pairs = []
    # 
    # first, try to pair reads that map to the same transcript or 
    # cluster or overlapping transcripts
    #
    same_strand = LibraryTypes.same_strand(library_type)
    refdict, clusterdict = map_reads_to_references(pe_reads, tid_tx_map)
    found_pair = False
    for tid, tid_pe_reads in refdict.iteritems():
        # check if there are alignments involving both reads in a pair
        if len(tid_pe_reads[0]) == 0 or len(tid_pe_reads[1]) == 0:
            # no paired alignments exist at this reference
            continue
        for r1 in tid_pe_reads[0]:
            for r2 in tid_pe_reads[1]:
                # read strands must agree with library type
                strand_match = (same_strand == (r1.is_reverse == r2.is_reverse))
                # these reads can be paired
                found_pair = True
                cr1 = copy_read(r1)
                cr2 = copy_read(r2)                    
                # this is a hit to same transcript (gene)
                # pair the reads if strand comparison is correct
                if strand_match:
                    tags = [(DISCORDANT_TAG_NAME, DiscordantTags.CONCORDANT_TX)]
                    concordant_tx_pairs.append((cr1,cr2))
                else:
                    # hit to same gene with wrong strand, which
                    # could happen in certain wacky cases
                    tags = [(DISCORDANT_TAG_NAME, DiscordantTags.DISCORDANT_STRAND_TX)]
                    discordant_tx_pairs.append((cr1,cr2))
                pair_reads(cr1,cr2,tags)
    # at this point, if we have not been able to find a suitable way
    # to pair the reads, then search within the transcript cluster
    if not found_pair:
        for cluster_id, cluster_pe_reads in clusterdict.iteritems():
            # check if there are alignments involving both reads in a pair
            if len(cluster_pe_reads[0]) == 0 or len(cluster_pe_reads[1]) == 0:
                # no paired alignments in this transcript cluster            
                continue
            for r1 in cluster_pe_reads[0]:
                for r2 in cluster_pe_reads[1]:
                    # check strand compatibility
                    strand_match = (same_strand == (r1.is_reverse == r2.is_reverse))
                    # these reads can be paired
                    found_pair = True
                    cr1 = copy_read(r1)
                    cr2 = copy_read(r2)                    
                    if strand_match:
                        tags = [(DISCORDANT_TAG_NAME, DiscordantTags.CONCORDANT_GENE)]
                        concordant_cluster_pairs.append((cr1,cr2))
                    else:
                        tags = [(DISCORDANT_TAG_NAME, DiscordantTags.DISCORDANT_STRAND_GENE)]
                        discordant_cluster_pairs.append((cr1,cr2))
                    pair_reads(cr1,cr2,tags)
    # at this point, we have tried all combinations.  if any paired reads
    # are concordant then return them without considering discordant reads 
    gene_pairs = []
    if len(concordant_tx_pairs) > 0:
        gene_pairs = concordant_tx_pairs
    elif len(concordant_cluster_pairs) > 0:
        gene_pairs = concordant_cluster_pairs
    if len(gene_pairs) > 0:
        return gene_pairs, []
    # if no concordant reads in transcripts, return any discordant reads 
    # that may violate strand requirements but still remain colocalized 
    # on the same gene/chromosome
    gene_pairs = []
    if len(discordant_tx_pairs) > 0:
        gene_pairs = discordant_tx_pairs
    elif len(discordant_cluster_pairs) > 0:
        gene_pairs = discordant_cluster_pairs    
    if len(gene_pairs) > 0:
        return gene_pairs, []
    #
    # at this point, no read pairings were found so the read is 
    # assumed to be discordant. now we can create all valid 
    # combinations of read1/read2 as putative discordant read pairs 
    #    
    gene_pairs = find_discordant_pairs(pe_reads, library_type)
    if len(gene_pairs) > 0:        
        # sort valid pairs by sum of alignment score and retain the best 
        # scoring pairs
        gene_pairs = select_best_scoring_pairs(gene_pairs)
        return gene_pairs, []
    # 
    # no valid pairs could be found suggesting that these mappings are
    # either mapping artifacts or that the current gene annotation set
    # lacks annotations support this pair
    # 
    return [], pe_reads

def write_pe_reads(pe_reads, bamfh):
    for reads in pe_reads:
        for r in reads:
            bamfh.write(r)

def write_pairs(pairs, pairedfh, discordantfh):
    for r1,r2 in pairs:
        # TODO: for now we are only going to deal with gene-gene
        # chimeras and leave other chimeras for study at a 
        # later time
        dr1 = r1.opt(DISCORDANT_TAG_NAME)
        dr2 = r2.opt(DISCORDANT_TAG_NAME)
        if (dr1 != DiscordantTags.DISCORDANT_GENE or
            dr2 != DiscordantTags.DISCORDANT_GENE):            
            pairedfh.write(r1)
            pairedfh.write(r2)
        else:
            discordantfh.write(r1)
            discordantfh.write(r2)

def find_discordant_fragments(transcripts,
                              input_bam_file, 
                              paired_bam_file, 
                              discordant_bam_file,
                              unpaired_bam_file,
                              multimap_bam_file,
                              max_isize, 
                              max_multihits,
                              library_type):
    """
    parses BAM file and categorizes reads into several groups:
    - concordant
    - discordant within gene (splicing isoforms)
    - discordant between different genes (chimeras)
    """
    logging.info("Finding discordant read pair combinations")
    logging.debug("\tInput file: %s" % (input_bam_file))
    logging.debug("\tMax insert size: '%d'" % (max_isize))
    logging.debug("\tLibrary type: '%s'" % (library_type))
    logging.debug("\tPaired BAM file: %s" % (paired_bam_file))
    logging.debug("\tUnpaired BAM file: %s" % (unpaired_bam_file))
    logging.debug("\tMultimap BAM file: %s" % (multimap_bam_file))
    # setup input and output files
    bamfh = pysam.Samfile(input_bam_file, "rb")
    pairedfh = pysam.Samfile(paired_bam_file, "wb", template=bamfh)
    discordantfh = pysam.Samfile(discordant_bam_file, "wb", template=bamfh)
    unpairedfh = pysam.Samfile(unpaired_bam_file, "wb", template=bamfh)
    multimapfh = pysam.Samfile(multimap_bam_file, "wb", template=bamfh)
    # build a lookup table from bam tid index to transcript object
    logging.debug("Building transcript lookup tables")
    tid_tx_map = build_tid_transcript_map(bamfh, transcripts)
    tid_tx_genome_map = build_tid_transcript_genome_map(bamfh, transcripts)
    # build a transcript to genome coordinate map
    logging.debug("Parsing reads")
    for pe_reads in parse_pe_reads(bamfh):
        # count multimapping
        mate_num_hits = [0, 0]
        for rnum,reads in enumerate(pe_reads):
            num_hits = count_transcriptome_multimaps(bamfh, reads, tid_tx_genome_map)
            mate_num_hits[rnum] = num_hits
        if min(mate_num_hits) == 0:
            # if either mate is unmapped then write the reads to the
            # unpaired bam file
            write_pe_reads(unpairedfh, pe_reads)
        elif max(mate_num_hits) > max_multihits:
            # if either mate has many genome mappings then write
            # the reads to the multimapping bam file
            write_pe_reads(multimapfh, pe_reads)
        else:
            # examine all read pairing combinations and rule out invalid pairings
            gene_pairs, unpaired_reads = classify_read_pairs(pe_reads, 
                                                             max_isize,
                                                             library_type, 
                                                             tid_tx_map)        
            if len(gene_pairs) > 0:
                write_pairs(gene_pairs, pairedfh, discordantfh)
            else:
                write_pe_reads(unpaired_reads, unpairedfh)
    pairedfh.close()
    unpairedfh.close()
    multimapfh.close()
    bamfh.close()  
    logging.info("Finished pairing reads")
    return config.JOB_SUCCESS

def main():
    logging.basicConfig(level=logging.DEBUG,
                        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument('--max-fragment-length', dest="max_fragment_length", 
                        type=int, default=config.DEFAULT_MAX_FRAG_LENGTH)
    parser.add_argument('--library', dest="library_type", 
                        default=LibraryTypes.FR_UNSTRANDED)
    parser.add_argument('--max-multihits', dest="max_multihits", 
                        default=config.DEFAULT_MAX_MULTIHITS)
    parser.add_argument("transcript_file")
    parser.add_argument("input_bam_file")
    parser.add_argument("paired_bam_file")
    parser.add_argument("discordant_bam_file")
    parser.add_argument("unpaired_bam_file")
    parser.add_argument("multimap_bam_file")
    args = parser.parse_args()    
    # read transcript features
    logging.debug("Reading transcript features")
    transcripts = list(TranscriptFeature.parse(open(args.transcript_file)))
    return find_discordant_fragments(transcripts,
                                     args.input_bam_file, 
                                     args.paired_bam_file,
                                     args.discordant_bam_file,
                                     args.unpaired_bam_file, 
                                     args.multimap_bam_file,
                                     max_isize=args.max_fragment_length,
                                     max_multihits=args.max_multihits,
                                     library_type=args.library_type)

if __name__ == '__main__':
    sys.exit(main())