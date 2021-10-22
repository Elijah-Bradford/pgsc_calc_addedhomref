// Import generic module functions
include { initOptions; saveFiles; getSoftwareName; getProcessName } from './functions'

params.options = [:]
options        = initOptions(params.options)

process PLINK_EXTRACT {
    tag "$meta.id"
    label 'process_low'
    publishDir "${params.outdir}",
        mode: params.publish_dir_mode,
        saveAs: { filename -> saveFiles(filename:filename, options:params.options, publish_dir:getSoftwareName(task.process), meta:meta, publish_by_meta:['id']) }

    conda (params.enable_conda ? "bioconda::plink=1.90b6.21" : null)
    if (workflow.containerEngine == 'singularity' && !params.singularity_pull_docker_container) {
        container "https://depot.galaxyproject.org/singularity/plink:1.90b6.21--h779adbc_1"
    } else {
        container "quay.io/biocontainers/plink:1.90b6.21--h779adbc_1"
    }

    // renaming input files breaks plink (i.e. staging them with different name)
    input:
    tuple val(meta), path(bed)
    tuple val(meta), path(bim)
    tuple val(meta), path(fam)
    path(variants)

    output:
    tuple val(meta), path("*.bed"), emit: bed
    tuple val(meta), path("*.bim"), emit: bim
    path "versions.yml"           , emit: versions

    script:
    def prefix   = options.suffix ? "${meta.id}${options.suffix}" : "${meta.id}"
    """
    plink --bfile ${bed.baseName} \\
        $options.args \\
        --extract ${variants} \\
        --threads $task.cpus \\
        --make-bed \\
        --out data

    cat <<-END_VERSIONS > versions.yml
    ${getProcessName(task.process)}:
        ${getSoftwareName(task.process)}: \$(echo \$(plink --version 2>&1) | sed 's/^PLINK v//' | sed 's/..-bit.*//' )
    END_VERSIONS
    """
}
