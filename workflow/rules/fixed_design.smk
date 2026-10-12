"""Optional fixed-design check of one comparison case (see converter/fixed_design.py and docs/consistency.md)."""


rule fixed_design:
    input:
        v06="<comparison_runs>/v06.nc",
        v07="<comparison_runs>/v07.nc",
    output:
        report="<fixed_design_report>",
    log:
        "<logs>/{model}/fixed_design_{case}.log",
    wildcard_constraints:
        model="[^/]+",
        case="[^/]+",
    conda:
        "../envs/module.yaml"
    message:
        "Fixed-design check of the runs `{wildcards.case}` of `{wildcards.model}` (optional `v07_fixed.nc`)."
    script:
        "../scripts/fixed_design_check.py"
