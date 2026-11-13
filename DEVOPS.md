# Message for DevOps

Copy everything below the line and send it, after `maps_merge.py` is merged into develop under `tools/`.

---

Hi,

We've added a Git merge tool for MAPS files and Simulink models to our repo at `tools/maps_merge.py`. Could you enable it for all users? It needs two one-time steps.

We added a merge script and changed two Git settings files. The script is `tools/maps_merge.py`, which merges MAPS files record by record.

The Git config file now registers two merge tools: our script for MAPS files, and MathWorks' mlAutoMerge for models, with a fix for a Linux library crash.

The `.git/info/attributes` file in each clone now sends `*.MAPS` files to our script and `*.mdl` files to mlAutoMerge, instead of treating them as binary. This file is local to each clone and never committed. The template in step 2 copies it into every new clone automatically.

The repo's `.gitattributes` stays as it is. With these in place, Git merges these files automatically during a normal `git merge` or `git pull`.

**1. Tell Git about the merge tools (system-wide).** Run this from the root of a clone of the repo:

```
git config --system merge.maps.name "MAPS record-level merge"
git config --system merge.maps.driver "/cadappl/python/2.7.18/python/bin/python tools/maps_merge.py merge %O %A %B -L %L -P %P"
git config --system merge.maps.recursive binary
git config --system diff.maps.textconv "/cadappl/python/2.7.18/python/bin/python tools/maps_merge.py diff"
/cadappl/python/2.7.18/python/bin/python tools/maps_merge.py setup --matlabroot /home/cadappl_RHEL8/matlab/2024b/matlab | grep '^git config' | grep -Ev '(merge|diff)\.maps\.' | sed 's/^git config /git config --system /' | sh
```

**2. Make every new clone use them** (`/cadappl/tools/git-template` is a suggestion; any shared folder all machines can read works):

```
mkdir -p /cadappl/tools/git-template/info
printf '%s\n' '*.MAPS merge=maps diff=maps' '*.maps merge=maps diff=maps' '*.mdl binary merge=mlAutoMerge' '*.slx binary merge=mlAutoMerge' > /cadappl/tools/git-template/info/attributes
git config --system init.templateDir /cadappl/tools/git-template
```

If `init.templateDir` is already set, please put the `info/attributes` file in that folder instead.

Please don't change the repo's `.gitattributes`. It must stay `binary`.

**Check:** in a fresh clone, `git check-attr merge -- <any .MAPS file>` should print `merge: maps`.

I'll ask the team to run `git init` once in their existing clones.

Thanks,
Bernard
