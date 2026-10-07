# Fixtures

`opening-blank-v1.json` is a manually constructed, complete domain fixture with
the standard 100-tile English distribution. The board has a blank assigned to
Q and an ordinary I; the first player's rack also contains a physical blank.
It retains private bag order and dummy opaque RNG state for schema round-trips.
The fixture RNG and lexicon identifiers are placeholders, not native data.

`identity-v1-vectors.json` fixes the player-0 observation ID and six action IDs
for that fixture, including pass, duplicate-tile exchanges, ordinary versus
blank Q placements, and a two-tile placement. These are compatibility vectors,
not claims of legal or strong play. Changing their hashes requires an intentional
identity-version/migration decision, not regenerating them to silence failures.

This fixture tests data integrity and privacy, not engine-verified scoring or
legality. Future native-output fixtures must record the engine commit and
command configuration that produced them. Test lexicons must be separate from
the full lexicon used for meaningful play.
