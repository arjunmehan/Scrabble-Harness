# Prompt snapshots

`decision-prompt-v1.txt` and `selection-with-evidence-v1.txt` freeze the initial
and final selection presentation for the deterministic synthetic contract
fixtures in `tests/harness/conftest.py`. These pools and analysis values test
formatting and validation; they are not native legality or strength results.

The fixtures cover a board blank, premiums, candidate handles, public inventory,
history, limits, and issued estimated evidence. Update them deliberately when
changing presentation, and change the prompt version when changing the
experimental protocol after it is frozen for evaluation.

`test_prompts_native.py` independently checks the premium map against all 225
native squares and the full NWL23 prepare/compare/final-selection protocol.
