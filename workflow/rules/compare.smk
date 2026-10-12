"""Compare a legacy Calliope v0.6 run with a run of the converted v0.7 model (runs are user inputs)."""


rule compare:
    input:
        v06="<comparison_runs>/v06.nc",
        v07="<comparison_runs>/v07.nc",
    output:
        report="<comparison_report>",
    log:
        "<logs>/{model}/compare_{case}.log",
    wildcard_constraints:
        model="[^/]+",
        case="[^/]+",
    conda:
        "../envs/module.yaml"
    message:
        "Compare the v0.6 and v0.7 runs `{wildcards.case}` of `{wildcards.model}`."
    script:
        "../scripts/compare_runs.py"
