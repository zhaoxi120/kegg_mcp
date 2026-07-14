# DeepKOALA guidance

DeepKOALA is an optional external annotator, not part of `kegg-mcp`. The commands and fields below
were checked against the official repository and GenomeNet page on 2026-07-14. Recheck them when a
different DeepKOALA version is used.

## Select a model and retain detailed output

For expected complete proteins:

```bash
python3 -m deepkoala.cli -i proteins.fasta -o results.csv --model full --detail --device cpu
```

For fragmented proteins, including many metagenomic gene predictions:

```bash
python3 -m deepkoala.cli -i fragments.fasta -o results.csv --model frag --detail --device cpu
```

Use `--detail`. The simple output omits below-threshold candidate evidence. Preserve the exact
command, DeepKOALA version, model choice, model artifact or date, execution date, and relevant
resource versions. Do not infer a version that was not recorded.

Detailed output currently includes `name`, `predict_label`, `probability`, `threshold`, and
`annotate`. Multi-domain mode may also include `start` and `end`. Preserve top-k and repeated rows
as separate evidence records.

Treat `--multi` as an advanced option for likely multi-domain proteins. It additionally requires
HMMER and KOfam profiles; verify their installation, access terms, and versions separately. Do not
recommend it merely because a sequence is long.

## Interpret source decisions

- Treat `annotate == "*"`, or a verified `probability >= threshold` source rule for that version,
  as source-accepted.
- A prediction below its source threshold is source-rejected with reason
  `below_source_threshold`, not automatically uncertain.
- Treat a row with no usable prediction as unclassified and a malformed K number as invalid.
- Do not interpret accepted as experimentally validated or rejected as functional absence.
- Do not compare probabilities across different models or model/database versions without an
  explicit compatibility basis.

Sources:

- <https://github.com/zhaoxi120/deepkoala>
- <https://www.genome.jp/tools/deepkoala/>
