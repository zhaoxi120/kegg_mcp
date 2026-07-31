# External handoff compatibility fixtures

These synthetic golden files exercise the documented upload shapes without containing KEGG
responses or downloaded KEGG assets.

- `mapper_color.tsv` follows the two-column `identifier<TAB>bgcolor,fgcolor` shape and includes
  the documented named color `skyblue`.
- `syntax_ko_sequence.tsv` follows the ordered two-column
  `caller_gene_id<TAB>assigned_K_number` shape.

The format requirements were checked against the official
[KEGG Mapper](https://www.kegg.jp/kegg/mapper/),
[KEGG Mapper Color](https://www.kegg.jp/kegg/mapper/color.html), and
[KEGG Syntax user-data analysis](https://www.kegg.jp/kegg/syntax/synteny.html) pages on
2026-07-31.
