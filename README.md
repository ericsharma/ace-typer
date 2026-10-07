# ace-typer

Types Pokémon FireRed/LeafGreen arbitrary-code-execution (ACE) box codes into
the PC box names for you. Built and tested on the English Switch rerelease of
FireRed on a Switch 2.

It drives a wired controller: an ESP32-S3 board flashed with
[Pokémon Automation](https://github.com/PokemonAutomation/ComputerControl)'s
PABotBase2 firmware, plugged into the Switch dock. The board times every press
itself and reports when each one finished, so a busy computer cannot shorten,
merge or drop a press. An [nxbt](https://github.com/Brikwerk/nxbt) Bluetooth
controller still works with `--controller nxbt`.

## Type a code (wired board)

1. Set up the board once with Pokémon Automation: flash the PABotBase2
   firmware, select `Serial: PABotBase2` and `NS1: Wired Pro Controller`, and
   press a button so the Switch shows it as **player 1**. Turn off other
   controllers: FireRed reads player 1 only.
2. In the game, open the PC → **MOVE POKéMON**. Put the cursor on the **title**
   of the first box to type (usually Box 1). The hand must be empty.
3. Stop Pokémon Automation: only one program can use the board.
   ```bash
   systemctl --user stop pokemon-automation
   ```
4. Check the board, then preview and type the code, on the computer the
   board's COM port is plugged into:
   ```bash
   ./run ace_typer.wired                                  # no input sent
   ./run ace_typer.type_code examples/codegenerator-switch.txt --dry
   ./run ace_typer.type_code examples/codegenerator-switch.txt
   ```
5. When it is done, compare every box name with the preview **before** you
   trigger ACE.

It refuses to start unless the Switch reads the board as player 1: a
controller the Switch has not accepted yet would lose its first press. After
the run it prints how long the board held every press; any press more than
10 ms off its plan is listed and the command exits with an error. Ctrl-C
clears the board's queue and releases all buttons.

## Web page (wired board)

```bash
./run ace_typer.server --host 127.0.0.1 --port 8171
```

Paste a code, select **Preview**, check the table, then **Type Box N** (one
box, then it moves to the next box so you can check each one) or **Type from
Box N** (all boxes). The page shows the board and Pokémon Automation status,
and can stop or start Pokémon Automation, which must be stopped while typing.
**Fast** (also `type_code --fast`) holds each press 100 ms with 150 ms between
presses instead of 150/370 ms; the waits at screen changes stay the same. It
is for the wired board only and still being tested: check every name.
**Stop** clears the board's queue and releases all buttons. It has no login:
anyone who can reach the page can press buttons on the Switch.

## Type a code (nxbt)

1. Connect the nxbt controller to the Switch.
2. Put the cursor on the first box title, as above.
3. On the nxbt web page, paste the code into **ACE Box Codes** and select
   **Preview**. Check the table: spaces show as `␣`.
4. Select **Type code**. Do not press keys on the page while it types.
5. Compare every box name with the preview before you trigger ACE.

**Stop** cancels the run and releases all buttons. **Start at** lets you begin
at a later box, for example to retype one. From the command line, add
`--controller nxbt` to `type_code`.

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
- `macro.py` turns the plan into timed steps: 150 ms hold (key repeat starts
  at 267 ms) and 370 ms between presses.
- `wired.py` sends the steps to the board's command queue; `pabb2.py` is the
  PABotBase2 serial protocol (ported from PA's `Common/PABotBase2`).
- `server.py` and `static/index.html` are the wired web page.
- `web.py` is what both web pages call; `send.py` is the nxbt
  command-line client.

The nxbt web app needs a small patch for this (the `macro_async`,
`macro_done`, `macro_clear`, `ace_preview` and `ace_type` socket events and
the panel). See the author's NixOS configuration.

## Tests

```bash
./run pytest -q tests
```
