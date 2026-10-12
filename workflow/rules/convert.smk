"""Convert legacy Calliope v0.6 models into Calliope v0.7 models."""


rule convert:
    input:
        legacy="<legacy_model>",
    output:
        model=directory("<converted_model>"),
        report="<conversion_report>",
        report_json="<conversion_report_json>",
    log:
        "<logs>/{model}/convert.log",
    wildcard_constraints:
        model="[^/]+",
    conda:
        "../envs/module.yaml"
    params:
        entry=lambda wc: (config["models"].get(wc.model) or {}).get("entry"),
        exclude_entries=config["exclude_entries"],
        copy_csvs=config["copy_csvs"],
        trim_timeseries=config["trim_timeseries"],
        contract_group_families=config["contract_group_families"],
    message:
        "Convert the legacy Calliope v0.6 model `{wildcards.model}` to Calliope v0.7."
    script:
        "../scripts/convert_model.py"


rule convert_all:
    default_target: False
    input:
        expand("<conversion_report>", model=list(config["models"])),
    message:
        "Convert every legacy model listed in the configuration."
