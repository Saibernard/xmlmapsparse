function test_mdl_merge(mdlPath)
% TEST_MDL_MERGE  Check that MathWorks' automatic MDL merge works here.
%   test_mdl_merge('RQxSV.mdl')
% Works on throwaway copies in a temp folder. The original is never changed.
% Needs R2019b or later.

work = fullfile(tempdir, sprintf('mdlmerge_%d', round(now * 1e5)));
mkdir(work);
[~, name] = fileparts(mdlPath);
P = @(n) fullfile(work, [n '.mdl']);

% base copy
load_system(mdlPath);
save_system(name, P('mt_base'));
subs = find_system('mt_base', 'SearchDepth', 1, 'BlockType', 'SubSystem');
subs = [subs(:); {'mt_base'; 'mt_base'}];      % fall back to top level if < 2 subsystems
close_system('mt_base', 0);

% mine: add a block in subsystem 1
load_system(P('mt_base')); save_system('mt_base', P('mt_mine'));
add_block('simulink/Sinks/Terminator', [regexprep(subs{1}, '^mt_base', 'mt_mine') '/MERGE_TEST_MINE']);
save_system('mt_mine'); close_system('mt_mine', 0);

% theirs: add a block in subsystem 2
load_system(P('mt_base')); save_system('mt_base', P('mt_theirs'));
add_block('simulink/Sinks/Terminator', [regexprep(subs{2}, '^mt_base', 'mt_theirs') '/MERGE_TEST_THEIRS']);
save_system('mt_theirs'); close_system('mt_theirs', 0);

% merge: same call Git makes  (base, ours, theirs, output)
exe = fullfile(matlabroot, 'bin', computer('arch'), 'mlAutoMerge');
if ispc, exe = [exe '.bat']; end
copyfile(P('mt_mine'), P('mt_merged'));
cmd = sprintf('"%s" "%s" "%s" "%s" "%s"', exe, P('mt_base'), P('mt_merged'), P('mt_theirs'), P('mt_merged'));
[status, out] = system(cmd);

if status ~= 0
    fprintf('AUTO-MERGE DID NOT COMPLETE (exit %d):\n%s\n', status, out);
    fprintf('Opening the three-way merge tool so you can see why...\n');
    slxmlcomp.slMerge(P('mt_base'), P('mt_mine'), P('mt_theirs'), P('mt_merged'));
    return
end

load_system(P('mt_merged'));
ok1 = ~isempty(find_system('mt_merged', 'Name', 'MERGE_TEST_MINE'));
ok2 = ~isempty(find_system('mt_merged', 'Name', 'MERGE_TEST_THEIRS'));
close_system('mt_merged', 0);
if ok1 && ok2
    fprintf('PASS: both additions present in %s\n', P('mt_merged'));
else
    fprintf('FAIL: mine present=%d, theirs present=%d\n', ok1, ok2);
end

% clash: both sides add a different block under the same name in the same place
load_system(P('mt_base')); save_system('mt_base', P('mt_mine2'));
add_block('simulink/Sinks/Terminator', [regexprep(subs{1}, '^mt_base', 'mt_mine2') '/MERGE_TEST_SAME']);
save_system('mt_mine2'); close_system('mt_mine2', 0);
load_system(P('mt_base')); save_system('mt_base', P('mt_theirs2'));
add_block('simulink/Math Operations/Gain', [regexprep(subs{1}, '^mt_base', 'mt_theirs2') '/MERGE_TEST_SAME']);
save_system('mt_theirs2'); close_system('mt_theirs2', 0);
copyfile(P('mt_mine2'), P('mt_merged2'));
cmd = sprintf('"%s" "%s" "%s" "%s" "%s"', exe, P('mt_base'), P('mt_merged2'), P('mt_theirs2'), P('mt_merged2'));
status2 = system(cmd);
if status2 ~= 0
    fprintf('PASS: clash was reported as a conflict, not silently merged.\n');
else
    fprintf('WARNING: clash merged silently. Check %s by hand.\n', P('mt_merged2'));
end
fprintf('Temp files are in %s\n', work);
end
