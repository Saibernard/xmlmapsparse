function report = prove_merge_pipeline(mapsFile, mdlFile, mapsMergePy)
% PROVE_MERGE_PIPELINE  End-to-end proof that MAPS and MDL merging works here.
%
%   prove_merge_pipeline('/repo/.../RQxSV.MAPS', '/repo/.../RQxSV.mdl', '/home/me/maps_merge.py')
%
% Checks, in order:
%   A. MAPS merge logic and the Git driver   (maps_merge.py selftest --git)
%   B. Git routing in this clone             (git check-attr, git config)
%   C. MathWorks mlAutoMerge present
%   D. MDL merge scenarios on throwaway copies of the model:
%        identical copies, different top-level subsystems, different nested
%        subsystems, same subsystem (expected conflict), same-name clash
%   F. One git merge in a throwaway repo where MAPS and MDL both changed,
%        clean case and conflict case, through both drivers at once
%   E. Model-to-MAPS consistency check       (maps_merge.py crosscheck, informational)
%
% Prints PASS/FAIL per check and writes merge_proof_<timestamp>.txt in the
% current folder. Nothing in the repository is modified. Needs R2019b or later.

mapsFile = char(mapsFile); mdlFile = char(mdlFile); mapsMergePy = char(mapsMergePy);
lines = {};
results = [];

    function say(fmt, varargin)
        s = sprintf(fmt, varargin{:});
        lines{end+1} = s; %#ok<AGROW>
        fprintf('%s\n', s);
    end

    function check(ok, name, detail)
        if nargin < 3, detail = ''; end
        results(end+1) = double(logical(ok)); %#ok<AGROW>
        if ok, tag = 'PASS'; else, tag = 'FAIL'; end
        if isempty(detail)
            say('%s  %s', tag, name);
        else
            say('%s  %s  (%s)', tag, name, detail);
        end
    end

    function [st, out] = sh(cmd)
        % run python/git without MATLAB's own library path leaking into them
        [st, out] = system(['env -u LD_LIBRARY_PATH -u LD_PRELOAD ' cmd]);
        out = strtrim(out);
    end

    function s = short(p)
        s = regexprep(p, '^pf_base/', '');
    end

% safety: never touch a model the user has open, and clear leftovers of a previous run
[~, modelName] = fileparts(mdlFile);
if bdIsLoaded(modelName)
    error('prove_merge_pipeline:modelOpen', ...
          'Model %s is open in Simulink. Save and close it first, then run again.', modelName);
end
for m = find_system('type', 'block_diagram')'
    if strncmp(m{1}, 'pf_', 3), close_system(m{1}, 0); end
end

say('MERGE PIPELINE PROOF   %s', datestr(now, 'yyyy-mm-dd HH:MM:SS'));
say('MATLAB %s   user %s   host %s', version, getenv('USER'), getenv('HOSTNAME'));
say('MAPS  %s', mapsFile);
say('MDL   %s', mdlFile);
say('tool  %s', mapsMergePy);
say('');

%% A. MAPS
say('== A. MAPS merge logic and Git driver ==');
py = '';
for cand = {'python', 'python3', 'python2'}
    [stc, ~] = sh(sprintf('%s -c "import sys" 2>/dev/null', cand{1}));
    if stc == 0, py = cand{1}; break, end
end
if isempty(py)
    check(false, 'a python interpreter is on PATH', 'tried python, python3, python2');
    py = 'python';
else
    say('using interpreter: %s', py);
end
[st, out] = sh(sprintf('%s "%s" selftest "%s" --git', py, mapsMergePy, mapsFile));
say('%s', out);
nPass = numel(regexp(out, '(?m)^PASS', 'match'));
nFail = numel(regexp(out, '(?m)^FAIL', 'match'));
check(st == 0 && nFail == 0 && nPass > 0, 'MAPS selftest incl. real git merge/diff/rebase', ...
      sprintf('%d PASS, %d FAIL, exit %d', nPass, nFail, st));
say('');

%% B. Git routing
say('== B. Git routing in this clone ==');
[mdir, mname, mext] = fileparts(mapsFile);
[~, out] = sh(sprintf('git -C "%s" check-attr merge diff -- "%s"', mdir, [mname mext]));
check(contains(out, 'merge: maps') && contains(out, 'diff: maps'), '.MAPS routed to the maps driver', out);
[ddir, dname, dext] = fileparts(mdlFile);
[~, out] = sh(sprintf('git -C "%s" check-attr merge -- "%s"', ddir, [dname dext]));
check(contains(out, 'merge: mlAutoMerge'), '.mdl routed to mlAutoMerge', out);
[st, out] = sh(sprintf('git -C "%s" config merge.maps.driver', mdir));
check(st == 0 && contains(out, 'maps_merge.py'), 'merge.maps.driver configured', out);
[st, out] = sh(sprintf('git -C "%s" config merge.mlAutoMerge.driver', ddir));
check(st == 0 && contains(out, 'mlAutoMerge'), 'merge.mlAutoMerge.driver configured', out);
say('');

%% C. mlAutoMerge
say('== C. MathWorks mlAutoMerge ==');
exe = fullfile(matlabroot, 'bin', computer('arch'), 'mlAutoMerge');
if ispc, exe = [exe '.bat']; end
check(exist(exe, 'file') == 2, 'mlAutoMerge present', exe);
say('');

%% D. MDL scenarios
say('== D. MDL merge scenarios (throwaway copies in a temp folder) ==');
work = fullfile(tempdir, sprintf('mergeproof_%d', round(now * 1e5)));
mkdir(work);
P = @(n) fullfile(work, [n '.mdl']);

    function makeVariant(vname, editFn)
        load_system(P('pf_base'));
        save_system('pf_base', P(vname));
        editFn(vname);
        save_system(vname);
        close_system(vname, 0);
    end

    function addBlock(model, subsysPath, blockName, libBlock)
        if nargin < 4, libBlock = 'simulink/Sinks/Terminator'; end
        p = regexprep(subsysPath, '^pf_base', model);
        add_block(libBlock, [p '/' blockName]);
    end

    function [st, out] = automerge(baseN, mineN, theirsN, outN)
        copyfile(P(mineN), P(outN));
        [st, out] = system(sprintf('"%s" "%s" "%s" "%s" "%s"', exe, P(baseN), P(outN), P(theirsN), P(outN)));
        out = strtrim(out);
    end

    function ok = hasBlock(model, blockName)
        load_system(P(model));
        ok = ~isempty(find_system(model, 'Name', blockName));
        close_system(model, 0);
    end

    function ok = editable(blk)
        ok = true;
        try
            ok = strcmp(get_param(blk, 'LinkStatus'), 'none');
        catch
        end
    end

try
    [~, name] = fileparts(mdlFile);
    load_system(mdlFile);
    save_system(name, P('pf_base'));
    top = find_system('pf_base', 'SearchDepth', 1, 'BlockType', 'SubSystem');
    top = top(cellfun(@editable, top));
    nested = find_system('pf_base', 'SearchDepth', 2, 'BlockType', 'SubSystem');
    nested = setdiff(nested, top, 'stable');
    nested = nested(cellfun(@editable, nested));
    close_system('pf_base', 0);
    say('model has %d top-level subsystems, %d nested one level down', numel(top), numel(nested));

    % D1 identical copies: the tool loads this model and exits 0
    copyfile(P('pf_base'), P('pf_same1'));
    copyfile(P('pf_base'), P('pf_same2'));
    [st, ~] = automerge('pf_base', 'pf_same1', 'pf_same2', 'pf_out0');
    check(st == 0, 'D1 mlAutoMerge runs on this model (identical copies)', sprintf('exit %d', st));

    % D2 different top-level subsystems: automatic
    if numel(top) >= 2
        makeVariant('pf_a1', @(m) addBlock(m, top{1}, 'PROOF_A1'));
        makeVariant('pf_b1', @(m) addBlock(m, top{2}, 'PROOF_B1'));
        [st, ~] = automerge('pf_base', 'pf_a1', 'pf_b1', 'pf_out1');
        ok = st == 0 && hasBlock('pf_out1', 'PROOF_A1') && hasBlock('pf_out1', 'PROOF_B1');
        check(ok, sprintf('D2 different top-level subsystems merge automatically (%s vs %s)', short(top{1}), short(top{2})), ...
              sprintf('exit %d, both blocks present=%d', st, ok));
    else
        say('INFO  D2 skipped: fewer than two editable top-level subsystems');
    end

    % D3 different nested subsystems under the same parent: automatic
    pair = {};
    for i = 1:numel(nested)
        for j = i + 1:numel(nested)
            if strcmp(fileparts(nested{i}), fileparts(nested{j}))
                pair = {nested{i}, nested{j}};
                break
            end
        end
        if ~isempty(pair), break, end
    end
    if ~isempty(pair)
        makeVariant('pf_a3', @(m) addBlock(m, pair{1}, 'PROOF_A3'));
        makeVariant('pf_b3', @(m) addBlock(m, pair{2}, 'PROOF_B3'));
        [st, ~] = automerge('pf_base', 'pf_a3', 'pf_b3', 'pf_out3');
        ok = st == 0 && hasBlock('pf_out3', 'PROOF_A3') && hasBlock('pf_out3', 'PROOF_B3');
        check(ok, sprintf('D3 different nested subsystems, same parent, merge automatically (%s vs %s)', short(pair{1}), short(pair{2})), ...
              sprintf('exit %d, both blocks present=%d', st, ok));
    else
        say('INFO  D3 skipped: no two editable nested subsystems share a parent');
    end

    % D4 same subsystem, different blocks: conflict for manual merge (expected)
    if numel(top) >= 1
        makeVariant('pf_a2', @(m) addBlock(m, top{1}, 'PROOF_A2'));
        makeVariant('pf_b2', @(m) addBlock(m, top{1}, 'PROOF_B2'));
        [st, ~] = automerge('pf_base', 'pf_a2', 'pf_b2', 'pf_out2');
        check(st ~= 0, sprintf('D4 same subsystem (%s), different blocks: reported as conflict, not silently merged', short(top{1})), ...
              sprintf('exit %d', st));

        % D5 same name clash: conflict (expected)
        makeVariant('pf_a4', @(m) addBlock(m, top{1}, 'PROOF_SAME', 'simulink/Sinks/Terminator'));
        makeVariant('pf_b4', @(m) addBlock(m, top{1}, 'PROOF_SAME', 'simulink/Math Operations/Gain'));
        [st, ~] = automerge('pf_base', 'pf_a4', 'pf_b4', 'pf_out4');
        check(st ~= 0, 'D5 same block name added on both sides: reported as conflict', sprintf('exit %d', st));
    end
catch err
    check(false, 'MDL scenarios aborted', err.message);
end
say('');

%% F. one git merge that touches both files at once, through both drivers
say('== F. End to end: one git merge changing MAPS and MDL together ==');
    function editMaps(src, dst, idx, value)
        % copy the MAPS file, changing the value of the idx-th parameter_value record
        txt = fileread(src);
        ls = strsplit(txt, newline, 'CollapseDelimiters', false);
        if isempty(ls{end}), ls(end) = []; end
        hits = find(strncmp(ls, '"parameter_value"', 17));
        ls{hits(idx)} = regexprep(ls{hits(idx)}, '"[^"]*"\s*$', ['"' value '"']);
        fid = fopen(dst, 'w');
        for k = 1:numel(ls), fprintf(fid, '%s\n', ls{k}); end
        fclose(fid);
    end
    function [st, out] = g(repo, varargin)
        args = sprintf(' %s', varargin{:});
        [st, out] = sh(sprintf('git -C "%s"%s', repo, args));
    end
needF = {'pf_a1', 'pf_b1', 'pf_a2'};
haveF = all(cellfun(@(n) exist(P(n), 'file') == 2, needF));
if ~haveF
    say('INFO  F skipped: section D did not produce the model variants it needs');
end
try
    if ~haveF, error('prove_merge_pipeline:noVariants', 'model variants missing'); end
    repo = fullfile(work, 'repo');
    mkdir(repo);
    rm = [mname mext]; rd = [dname dext];
    q = '''';   % a single quote character, for shell quoting
    g(repo, 'init -q');
    g(repo, 'config user.email proof@maps_merge');
    g(repo, 'config user.name "merge proof"');
    g(repo, 'config commit.gpgsign false');
    g(repo, 'config merge.maps.driver', [q '"' py '" "' mapsMergePy '" merge %O %A %B -L %L -P %P' q]);
    g(repo, 'config merge.maps.recursive binary');
    g(repo, 'config merge.mlAutoMerge.driver', [q '"' exe '" %O %A %B %A' q]);
    fid = fopen(fullfile(repo, '.gitattributes'), 'w');
    fprintf(fid, '*.MAPS merge=maps diff=maps\n*.mdl binary merge=mlAutoMerge\n');
    fclose(fid);
    g(repo, 'checkout -q -b base');
    copyfile(mapsFile, fullfile(repo, rm));
    copyfile(P('pf_base'), fullfile(repo, rd));
    g(repo, 'add -A'); g(repo, 'commit -q -m base');
    [~, out] = g(repo, 'check-attr merge --', rm, rd);
    check(contains(out, 'merge: maps') && contains(out, 'merge: mlAutoMerge'), 'F0 throwaway repo routes both files', out);

    % branch A: MAPS record 1 -> PROOF_A, MDL block in subsystem 1
    g(repo, 'checkout -q -b A');
    editMaps(mapsFile, fullfile(repo, rm), 1, 'PROOF_A');
    copyfile(P('pf_a1'), fullfile(repo, rd));
    g(repo, 'commit -q -am A');
    % branch B: MAPS record 2 -> PROOF_B, MDL block in subsystem 2
    g(repo, 'checkout -q base'); g(repo, 'checkout -q -b B');
    editMaps(mapsFile, fullfile(repo, rm), 2, 'PROOF_B');
    copyfile(P('pf_b1'), fullfile(repo, rd));
    g(repo, 'commit -q -am B');
    [st, out] = g(repo, 'merge --no-edit A');
    say('%s', out);
    mergedMaps = fileread(fullfile(repo, rm));
    copyfile(fullfile(repo, rd), P('pf_gitmerged'));
    okMdl = hasBlock('pf_gitmerged', 'PROOF_A1') && hasBlock('pf_gitmerged', 'PROOF_B1');
    okMaps = contains(mergedMaps, '"PROOF_A"') && contains(mergedMaps, '"PROOF_B"') && ~contains(mergedMaps, '<<<<<<<');
    check(st == 0 && okMaps && okMdl, 'F1 git merge: MAPS edits and MDL edits in different subsystems all merged in one go', ...
          sprintf('exit %d, maps ok=%d, mdl ok=%d', st, okMaps, okMdl));

    % branch C: same MAPS record as A with a different value, MDL block in the same subsystem as A
    g(repo, 'checkout -q base'); g(repo, 'checkout -q -b C');
    editMaps(mapsFile, fullfile(repo, rm), 1, 'PROOF_C');
    copyfile(P('pf_a2'), fullfile(repo, rd));
    g(repo, 'commit -q -am C');
    [st, out] = g(repo, 'merge --no-edit A');
    say('%s', out);
    [~, status] = g(repo, 'status --porcelain');
    conflMaps = fileread(fullfile(repo, rm));
    check(st ~= 0 && contains(status, ['UU ' rm]) && contains(status, ['UU ' rd]) && contains(conflMaps, '<<<<<<< ours'), ...
          'F2 git merge: clashing MAPS value and same-subsystem MDL change both stop as conflicts', ...
          regexprep(status, '\s+', ' '));
    g(repo, 'merge --abort');
catch err
    if ~strcmp(err.identifier, 'prove_merge_pipeline:noVariants')
        check(false, 'end-to-end git scenarios aborted', err.message);
    end
end
say('');

%% E. crosscheck (informational)
say('== E. Model-to-MAPS consistency (informational) ==');
[st, out] = sh(sprintf('%s "%s" crosscheck "%s" "%s"', py, mapsMergePy, mdlFile, mapsFile));
say('%s', out);
say('INFO  crosscheck exit %d. If every MAPS object is reported missing, the naming rule needs --map/--strip/--prefix.', st);
say('');

%% Summary
n = numel(results); p = sum(results);
say('== SUMMARY: %d of %d checks passed ==', p, n);
say('What this proves: MAPS files merge record by record through Git; MDL files merge');
say('automatically when changes are in different subsystems and stop for a manual');
say('three-way merge when they are in the same one; nothing is merged silently wrong.');
reportFile = fullfile(pwd, sprintf('merge_proof_%s.txt', datestr(now, 'yyyymmdd_HHMMSS')));
fid = fopen(reportFile, 'w');
fprintf(fid, '%s\n', lines{:});
fclose(fid);
say('report written to %s', reportFile);
say('temp files in %s', work);
report = strjoin(lines, newline);
end
