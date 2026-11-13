# Message for DevOps

Copy everything below the line and send it, after `maps_merge.py` is merged into develop under `tools/`.

---

Hi,

We've added a Git merge tool for MAPS files and Simulink models to our repo at `tools/maps_merge.py`. Could you enable it for all users? It needs two one-time steps.

**How it works:** Git lets you register a "merge driver", a command that Git itself runs during `git merge`, `git pull` and `git rebase` for certain file types. Today the repo's `.gitattributes` marks MAPS and model files as `binary`, so Git refuses to merge them and people redo their changes by hand. The steps below change two things:

- **Git config** registers the drivers: MAPS files go to our merger, which merges record by record, and models go to MathWorks' command-line merge (`mlAutoMerge`, with a fix for a Linux library crash).
- **A per-clone attributes file** routes `*.MAPS` and `*.mdl` to those drivers, overriding `binary` locally.

After that, merging needs no extra steps and no separate GUI. Git calls the driver automatically, which writes the merged file in place and tells Git whether it was clean or a conflict. The Three-Way Merge window is only needed when a model really clashes. The shared `.gitattributes` stays `binary`, so the server's merge button, which can't run these drivers, keeps refusing these files instead of text-merging them.

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
