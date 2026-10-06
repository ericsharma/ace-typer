# ace-typer

Types Pokémon FireRed/LeafGreen arbitrary-code-execution (ACE) box-name codes
into the PC box names, through an [nxbt](https://github.com/Brikwerk/nxbt)
virtual Pro Controller. Built and tested on the English Switch rerelease of
FireRed on a Switch 2.

## What it does

1. **Parses** pasted codes: [CodeGenerator](https://e-sh4rk.github.io/CodeGenerator/)
   output (box lines plus "Raw data"), pomeg-style character codes
   (`Box 2: P R o / F Q m _ [PRo/FQm ]`), and Hex Writer codes
   (`Box 3: C6E9D7DF`, `Boxes 10-14: 00000000`). When "Raw data" is present,
   every box name is encoded to game bytes and must match it exactly.
2. **Plans** the shortest D-pad + A path on the box-naming keyboard, using a
   model ported from [pret/pokefirered](https://github.com/pret/pokefirered)
   `naming_screen.c`, and replays it through that model to check the result.
3. **Types** it as one nxbt macro, so nxbt times every press.

Nothing is guessed: look-alike characters (`–` for `-`) are reported, and
ambiguous input (straight `"`, mismatched spaced/bracket forms) is refused.

## Use

Start state: PC → MOVE POKéMON, cursor on the title of the first box to type.

```bash
./run ace_typer.type_code CODE.txt --dry      # preview
./run ace_typer.type_code CODE.txt            # type all boxes
./run ace_typer.type_code CODE.txt --box 5    # one box, then move to the next
./run pytest -q tests
```

`ace_typer.web` exposes `preview()` and `compile()` for a web UI.

## Requirements on the nxbt side

The nxbt web app must provide the non-blocking `macro_async`, `macro_done`
and `macro_clear` socket events (a small patch; see the author's NixOS
configuration).

## Timing

150 ms hold, 250 ms gap. The naming screen starts key repeat after 16 frames
(267 ms), so longer holds double-press; shorter gaps merged presses.
