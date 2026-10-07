# Native smoke fixture

`NWL_HARNESS_SMOKE.txt` is an authored 20-word test lexicon. It is **not NWL**;
the prefix only selects MAGPIE's English defaults. It includes QI, several
short crossing words, and RETINAS for a bingo check. QA is deliberately absent.

The setup script builds a KWG and an empty English KLV with all leave values
zero. This data verifies interface behavior, scores, and resource semantics.
It cannot measure engine strength or LLM play quality. Do not publish it as a
representative Scrabble benchmark.
