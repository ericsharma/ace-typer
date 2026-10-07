# ace-typer

Types Pokémon FireRed/LeafGreen arbitrary-code-execution (ACE) box codes into
the PC box names for you, through an [nxbt](https://github.com/Brikwerk/nxbt)
virtual Pro Controller. Built and tested on the English Switch rerelease of
FireRed on a Switch 2.

## Type a code

1. Connect the nxbt controller to the Switch.
2. In the game, open the PC → **MOVE POKéMON**. Put the cursor on the **title**
   of the first box to type (usually Box 1). The hand must be empty.
3. On the nxbt web page, paste the code into **ACE Box Codes** and select
   **Preview**. Check the table: spaces show as `␣`.
4. Select **Type code**. Do not press keys on the page while it types.
5. When it is done, compare every box name with the preview **before** you
   trigger ACE.

**Stop** cancels the run and releases all buttons. **Start at** lets you begin
at a later box, for example to retype one.

## Codes it reads

- [CodeGenerator](https://e-sh4rk.github.io/CodeGenerator/index_frlg.html?lang=eng10)
  output. Use the "Switch" language for the Switch rerelease. When the output
  has "Raw data", every box name is checked against it byte for byte.
- Character codes: `Box 2: P R o / F Q m _ [PRo/FQm ]` (`_` is a space).
- Hex Writer codes: `Box 3: C6E9D7DF`, `Boxes 10-14: 00000000`. Placeholders
  such as `****` get input fields.

See [`examples/`](examples/).

## What it checks

- Nothing is guessed. Look-alike characters (`–` typed as `-`) are reported;
  ambiguous input (a straight `"`, spaced and bracket forms that disagree) is
  refused with the reason.
- With the Switch option on, it warns where the
  [pomeg FR/LG FAQ](https://pomeg-letterbombers.github.io/pokemon-ace-notes/frlg-faq/)
  says a code needs a Switch version (spaces at the end of Boxes 4, 8 and 12,
  and two exit codes that crash on the Switch). It never changes the code.

## Command line

```bash
./run ace_typer.type_code examples/codegenerator-switch.txt --dry     # preview
./run ace_typer.type_code examples/codegenerator-switch.txt           # type it
./run ace_typer.type_code examples/codegenerator-switch.txt --box 5   # one box
```

## How it works

- `parse.py` reads the pasted text into box names.
- `keyboard.py` models the game's naming screen (ported from
  [pret/pokefirered](https://github.com/pret/pokefirered) `naming_screen.c`),
  plans the shortest D-pad + A path for each name, and replays it to check it.
- `macro.py` turns the plan into one nxbt macro. nxbt times every press:
  150 ms hold (key repeat starts at 267 ms) and 370 ms between presses.
- `web.py` is what the nxbt web page calls; `send.py` is the command-line client.

The nxbt web app needs a small patch for this (the `macro_async`,
`macro_done`, `macro_clear`, `ace_preview` and `ace_type` socket events and
the panel). See the author's NixOS configuration.

## Tests

```bash
./run pytest -q tests
```
