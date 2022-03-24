#!/usr/bin/env python3

import polars as pl
import argparse
import sys

def parse_args(args=None):
    parser = argparse.ArgumentParser(description='Read and format scoring files')
    parser.add_argument('-d','--dataset', dest = 'dataset', required = True,
                        help='<Required> Label for target genomic dataset (e.g. "-d thousand_genomes")')
    parser.add_argument('-s','--scorefiles', dest = 'scorefile', required = True,
                        help='<Required> Combined scorefile path (output of read_scorefiles.py)')
    parser.add_argument('-t','--target', dest = 'target', required = True,
                        help='<Required> A table of target genomic variants (.bim format)')
    parser.add_argument('--split', dest = 'split', default=False, action='store_true',
                        help='<Required> Split scorefile per chromosome?')
    parser.add_argument('--format', dest = 'plink_format', help='<Required> bim or pvar?')
    parser.add_argument('-m', '--min_overlap', dest='min_overlap', required=True,
                        type = float, help='<Required> Minimum proportion of variants to match before error')
    return parser.parse_args(args)

def read_target(path, plink_format):
    """Complementing alleles with a pile of regexes seems weird, but polars string
    functions are limited (i.e. no str.translate). Applying a python complement
    function would be very slow compared to this, unless I develop a function
    in rust. I don't know rust, and I stole the regex idea from Scott.
    """
    if plink_format == 'bim':
        x = pl.read_csv(path, sep = '\t', has_header = False)
        x.columns = ['#CHROM', 'ID', 'CM', 'POS', 'REF', 'ALT']
    else:
        x = pl.read_csv(path, sep = '\t', has_header = True, comment_char = '##')

    x = x.with_columns([
        (pl.col("REF").str.replace_all("A", "V")
            .str.replace_all("T", "X")
            .str.replace_all("C", "Y")
            .str.replace_all("G", "Z")
            .str.replace_all("V", "T")
            .str.replace_all("X", "A")
            .str.replace_all("Y", "G")
            .str.replace_all("Z", "C"))
            .alias("REF_FLIP"),
        (pl.col("ALT").str.replace_all("A", "V")
            .str.replace_all("T", "X")
            .str.replace_all("C", "Y")
            .str.replace_all("G", "Z")
            .str.replace_all("V", "T")
            .str.replace_all("X", "A")
            .str.replace_all("Y", "G")
            .str.replace_all("Z", "C"))
            .alias("ALT_FLIP")
    ])

    return x.with_columns([
        pl.col("REF").cast(pl.Categorical),
        pl.col("ALT").cast(pl.Categorical),
        pl.col("ALT_FLIP").cast(pl.Categorical),
        pl.col("REF_FLIP").cast(pl.Categorical)])

def read_scorefile(path):
    scorefile = pl.read_csv(path, sep = '\t')
    return scorefile.with_columns([
        pl.col("effect_allele").cast(pl.Categorical),
        pl.col("other_allele").cast(pl.Categorical),
        pl.col("effect_type").cast(pl.Categorical),
        pl.col("accession").cast(pl.Categorical)
   ])

def match_variants(scorefile, target, EA, OA, match_type):
    colnames = ['chr_name', 'chr_position', 'effect_allele', 'other_allele', 'effect_weight', 'effect_type', 'accession', 'ID', 'REF', 'ALT', 'REF_FLIP', 'ALT_FLIP', 'match_type']

    matches = scorefile.join(target, left_on = ['chr_name', 'chr_position', 'effect_allele', 'other_allele'], right_on = ['#CHROM', 'POS', EA, OA], how = 'inner').with_columns([
        pl.col("*"),
        pl.col("effect_allele").alias(EA), # copy the column that's dropped by join
        pl.col("other_allele").alias(OA),
        pl.lit(match_type).alias("match_type")
        ])
    # join removes matching key, reorder columns for vertical stacking (pl.concat)
    # collecting is needed for reordering columns
    return matches[colnames]

def get_all_matches(target, scorefile, remove):
    """ Get intersection of variants using four different schemes, optionally
    removing ambiguous variants (default: true)

    scorefile      | target | scorefile   |  target
    effect_allele == REF and other_allele == ALT
    effect_allele == ALT and other_allele == REF
    effect_allele == flip(REF) and other_allele == flip(ALT)
    effect_allele == flip(REF) and oher_allele ==  flip(REF)
    """

    refalt = match_variants(scorefile, target, EA = 'REF', OA = 'ALT', match_type = "refalt")
    altref = match_variants(scorefile, target, EA = 'ALT', OA = 'REF', match_type = "altref")
    refalt_flip = match_variants(scorefile, target, EA = 'REF_FLIP', OA = 'ALT_FLIP', match_type = "refalt_flip")
    altref_flip = match_variants(scorefile, target, EA = 'ALT_FLIP', OA = 'REF_FLIP', match_type = "altref_flip")
    return label_biallelic_ambiguous(pl.concat([refalt, altref, refalt_flip, altref_flip]), remove)

def label_biallelic_ambiguous(matches, remove):
    # A / T or C / G may match multiple times
    matches = matches.with_columns([
        pl.col(["effect_allele", "other_allele", "REF", "ALT", "REF_FLIP", "ALT_FLIP"]).cast(str),
        pl.lit(True).alias("ambiguous")
    ])

    ambiguous = (matches.with_column(
        pl.when((pl.col("effect_allele") == pl.col("ALT_FLIP")) | \
                (pl.col("effect_allele") == pl.col("REF_FLIP")))
        .then(pl.col("ambiguous"))
        .otherwise(False)))

    if remove:
        return ambiguous.filter(pl.col("ambiguous") == False)
    else:
        return ambiguous

def unduplicate_variants(df):
    """ Find variant matches that have duplicate identifiers
    When merging a lot of scoring files, sometimes a variant might be duplicated
    this can happen when the effect allele differs at the same position, e.g.:
        - chr1: chr2:20003:A:C A 0.3 NA
        - chr1: chr2:20003:A:C C NA 0.7
    where the last two columns represent different scores.  plink demands
    unique identifiers! so need to split, score, and sum later
    Parameters:
    df: A dataframe containing all matches, with columns ID, effect_allele, and
        effect_weight
    Returns:
        A dict of data frames (keys 'first' and 'dup')
    """
    return {'first': df[~df["ID"].is_duplicated()], 'dup': df[df["ID"].is_duplicated()]}

def format_scorefile(df, split):
    """ Format a dataframe to plink2 --score standard

    Minimum example:
    ID | effect_allele | effect_weight

    Multiple scores are OK too:
    ID | effect_allele | weight_1 | ... | weight_n
    """
    if split:
        chroms = df["chr_name"].unique().to_list()
        return { x: (df.filter(pl.col("chr_name") == x)
                     .pivot(index = ["ID", "effect_allele"], values = "effect_weight", columns = "accession")
                     .fill_null(pl.lit(0)))
                 for x in chroms }
    else:
        return { 'false': (df.pivot(index = ["ID", "effect_allele"], values = "effect_weight", columns = "accession")
                           .fill_null(pl.lit(0))) }

def split_effect_type(df):
    effect_types = df["effect_type"].unique().to_list()
    return {x: df.filter(pl.col("effect_type") == x) for x in effect_types}

def write_scorefile(effect_type, scorefile, split):
    fout = '{chr}_{et}_{dup}.scorefile'

    if scorefile.get('first').shape[0] > 0:
        df_dict = format_scorefile(scorefile.get('first'), split)
        for k, v in df_dict.items():
            path = fout.format(chr = k, et = effect_type, dup = 'first')
            v.write_csv(path, sep = "\t")

    if scorefile.get('dup').shape[0] > 0:
        df_dict = format_scorefile(scorefile.get('dup'), split)
        for k, v in df_dict.items():
            path = fout.format(chr = k, et = effect_type, dup = 'first')
            v.write_csv(path, sep = "\t")

def main(args = None):
    ''' Match variants from scorefiles against target variant information '''
    pl.Config.set_global_string_cache()
    args = parse_args(args)

    # read inputs --------------------------------------------------------------
    target = read_target(args.target, args.plink_format)
    scorefile = read_scorefile(args.scorefile)

    # start matching -----------------------------------------------------------
    matches = get_all_matches(target, scorefile, remove = True)



    empty_err = ''' ERROR: No target variants match any variants in all scoring files
    This is quite odd!
    Try checking the genome build (see --liftover and --target_build parameters)
    Try imputing your microarray data if it doesn't cover the scoring variants well
    '''
    assert matches.shape[0] > 0, empty_err

    # prepare for writing out --------------------------------------------------
    # write one combined scorefile for efficiency, but need extra file for each:
    #     - effect type (e.g. additive, dominant, or recessive)
    #     - duplicated chr:pos:ref:alt ID (with different effect allele)
    ets = split_effect_type(matches)
    unduplicated = { k: unduplicate_variants(v) for k, v in ets.items() }
    ea_dict = { 'is_dominant': 'dominant', 'is_recessive': 'recessive', 'additive': 'additive'}
    [ write_scorefile(ea_dict.get(k), v, args.split) for k, v in unduplicated.items() ]

if __name__ == '__main__':
    sys.exit(main())
