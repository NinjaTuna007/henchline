# Phone legacy layout (synthetic)

The single-laptop app's storage, made up for tests of the multi-host import (`LegacyImport.kt`): `files/` is the
app's `filesDir`, `prefs-call.json` its "call" preferences (a JSON array stands for a string set, a number for a
long). Cases: a chat whose id the crew names (`cursor_chat_7f`), two ids that rewrite to one file name (`a_b`:
`a:b` and `a/b`, left unresolved), an id nobody knows (`orphan_9`), names that order differently as files and as
keys (`a`, `a-b`), a line that isn't JSON, a picture's small copy, media, calls, an outbox in order.
