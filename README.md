# maps_merge

Git merge support for the servo-group repo: MAPS files are merged record by
record by `maps_merge.py`, MDL/SLX models are merged by MathWorks' own
`mlAutoMerge`, and `maps_merge.py crosscheck` verifies that a merged model and
a merged MAPS file still agree.

One file does everything: `maps_merge.py`. Standard library only. Runs on
Python 2.7 and Python 3.x. Nothing is installed.

## Files

| file | purpose |
|---|---|
| `maps_merge.py` | the tool (merge driver, diff, inspect, check, selftest, crosscheck, setup) |
| `.gitattributes.example` | the attribute lines that route files to the merge drivers |
| `maps_merge.json.example` | optional key configuration, only needed if `inspect` shows a wrong key |
| `tests/` | test suite, run with `python -m unittest discover -s tests` |
| `test_mdl_merge.m` | optional MATLAB script that exercises `mlAutoMerge` on throwaway copies of a model |
| `prove_merge_pipeline.m` | MATLAB script that checks the whole pipeline end to end and writes a PASS/FAIL report |

## Private trial, in your own clone, invisible to everyone else

1. Copy `maps_merge.py` somewhere stable, for example `tools/maps_merge.py` in the clone.
2. In the clone run:

       python tools/maps_merge.py setup --apply --local-attributes --matlabroot /path/to/MATLAB/R2024b

   This runs the `git config` lines for this clone (merge driver, diff
   textconv, and `merge.maps.recursive=binary` so criss-cross merges work)
   and writes the attribute lines to `.git/info/attributes`. Both are local to your clone. To see the
   lines without applying them, drop `--apply`.
3. Check that Git now routes the files:

       git check-attr merge diff -- path/to/RQxSV.MAPS
       git check-attr merge -- path/to/RQxSV.mdl

   Expected: `merge: maps`, `diff: maps` and `merge: mlAutoMerge`.
4. Run the self-test on a real file. It works on temp copies; the file is never modified:

       python tools/maps_merge.py selftest path/to/RQxSV.MAPS

5. Run `inspect` on a real file and read the key table. If a record type shows
   `+` (key widened) or a suspicious width, copy `maps_merge.json.example` to
   `maps_merge.json` next to the script and set the width for that type.

       python tools/maps_merge.py inspect path/to/RQxSV.MAPS

6. Merge something real: two branches that both changed the same MAPS file,
   then `git merge` or `git pull`. Open the result in the MAPS editor.

To undo the trial: `git config --unset merge.maps.driver` (and the other
`merge.maps.*`, `diff.maps.*`, `merge.mlAutoMerge.*` keys) and delete
`.git/info/attributes`.

## Proving the whole thing in one go

From the MATLAB command window, with the three paths filled in:

    prove_merge_pipeline('/repo/.../RQxSV.MAPS', '/repo/.../RQxSV.mdl', '/home/you/maps_merge.py')

It prints a PASS/FAIL line per check and writes `merge_proof_<timestamp>.txt` in the
current folder. Nothing in the repository is modified and no branches are created
there; the Git scenarios run in a throwaway repository in a temp folder. Close the
model in Simulink first, or the script refuses to start.

What it checks:

| section | check |
|---|---|
| A | MAPS merge logic, plus real `git merge`, `git diff` and `git rebase` in a temp repo |
| B | this clone routes `.MAPS` to the maps driver and `.mdl` to `mlAutoMerge` |
| C | `mlAutoMerge` is present under this MATLAB |
| D | MDL scenarios on copies of the model: identical, different top-level subsystems, different nested subsystems, same subsystem (expected conflict), same-name clash (expected conflict) |
| F | one `git merge` where the MAPS file and the model both changed: clean case and conflict case, through both drivers at once |
| E | model-to-MAPS consistency (informational) |

## Team rollout

1. Commit `maps_merge.py` into the repo (for example under `tools/`).
2. In the committed `.gitattributes`, replace `*.MAPS binary` and `*.mdl binary`
   with the lines from `.gitattributes.example`. If you keep the old lines, the
   new ones must come after them: for Git the last matching line wins.
3. Every engineer runs once per clone:

       python tools/maps_merge.py setup --apply --matlabroot /path/to/MATLAB/R2024b

Anyone who has not run step 3 keeps today's behaviour (Git refuses to merge and
they pick a side), so the rollout can be gradual.

## What happens on a merge

`git pull`, `git merge`, `git rebase`, `git cherry-pick` and `git stash pop` all
call the driver whenever both sides changed the same MAPS file.

* Records changed on one side only are taken.
* The same record changed identically on both sides is taken once.
* The same record changed differently on both sides is a conflict, and so is a
  record edited on one side and deleted on the other, including when the edit
  renamed it. The file is
  written with git-style markers around only the clashing records, the merge
  stops, and `git status` shows the file as unmerged. Fix it in any text editor
  by keeping one of the lines and deleting the marker lines, then `git add` and
  commit. Everything else in the file is already merged.
* `history` records from both sides are all kept, in date order.
* `network_properties` `version_number` and the header version take the larger value.
* A line that is not a proper record (a field without quotes, a stray quote, a
  trailing comma) makes the driver refuse the merge with exit 2 and the line
  number, rather than guess.
* Lines nobody touched are copied byte for byte. Changed lines are copied byte
  for byte from the side that changed them. Nothing is reformatted.
* If a side cannot be parsed the driver exits 2, leaves your file untouched, and
  Git reports the file as conflicted for manual resolution.

Conflict block example:

    <<<<<<< ours
    "parameter_value", "ActSys.X", "gain", "", "", "Nominal", "Default", "2.5"
    ||||||| base
    "parameter_value", "ActSys.X", "gain", "", "", "Nominal", "Default", "1.0"
    =======
    "parameter_value", "ActSys.X", "gain", "", "", "Nominal", "Default", "3.0"
    >>>>>>> theirs

## Commands

    python maps_merge.py merge BASE OURS THEIRS [-o OUT] [-L 7] [-P name] [--check-cmd "cmd {file}"]
    python maps_merge.py diff FILE
    python maps_merge.py inspect FILE [--write-config maps_merge.json]
    python maps_merge.py check FILE [--run "cmd {file}"]
    python maps_merge.py selftest FILE [--keep]
    python maps_merge.py crosscheck MODEL.mdl FILE.MAPS [--strict] [--map dots|slash|leaf] [--strip N] [--prefix STR]
    python maps_merge.py setup [--apply] [--local-attributes] [--global] [--matlabroot DIR]

`merge` can be run by hand on three copied files, without Git at all:

    python maps_merge.py merge base.MAPS mine.MAPS theirs.MAPS -o merged.MAPS

`--check-cmd` runs a validator on the merged file, for example the makefile's
MAPS2DDF check; a non-zero exit turns the merge into a conflict so a broken
file never gets committed silently. The merged file is left in place, without
markers, so it can be inspected. Add it to the driver line in `git config`
if wanted.

## MDL, XML and the crosscheck

* MDL files saved by R2024b are MathWorks OPC text packages. They must never be
  text-merged. The attribute line routes them to `mlAutoMerge`, which merges at
  subsystem level and opens the Three-Way Merge tool when the same subsystem
  changed on both sides.
* The XML produced by the SGM-to-XML converter is generated. Do not merge it.
  Merge the MDL, rerun the converter, re-import into MAPS.
* After a merge that touched both the model and the MAPS file, run

      python maps_merge.py crosscheck RQxSV.mdl RQxSV.MAPS

  It lists MAPS network objects and connection endpoints that no longer exist
  in the model. If every object is reported, the naming rule is wrong for your
  models: try `--map leaf`, `--strip 1` or `--prefix`. `--show-model-only`
  lists model blocks without a MAPS object.

## GitHub

GitHub's web merge button does not run custom merge drivers. Merge locally
(or in CI) and push. The branch protection setting "require branches to be up
to date before merging" makes the button safe, because the local merge has
already happened.
