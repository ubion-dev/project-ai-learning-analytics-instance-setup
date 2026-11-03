"""Synthetic Linux process-title observations, never live proc or device data."""
import pytest

from ala.chrome_process import AmbiguousCommandLine, observe_chrome_cmdline, profile_evidence


EXE = '/opt/google/chrome/chrome'
ARGV = ('/usr/bin/google-chrome-stable',
        '--user-data-dir=/home/user/.config/ala/chrome-display', '--start-fullscreen',
        '--no-first-run', '--password-store=basic', '--remote-debugging-pipe',
        'data:text/html,synthetic-recovery')


def flattened(arguments=ARGV, *, executable=EXE):
    return ' '.join((executable, *arguments[1:])).encode()


@pytest.mark.parametrize('suffix', [b'\0', b'\0\0\0'])
def test_original_nul_argv_preserves_argument_boundaries(suffix):
    value = observe_chrome_cmdline(b'\0'.join(arg.encode() for arg in ARGV) + suffix,
                                  expected_arguments=ARGV[1:])
    assert value.format == 'nul_argv'
    assert value.arguments == ARGV


def test_original_argv_with_spaces_quotes_and_backslashes_is_not_shell_parsed():
    original = (ARGV[0], '--user-data-dir=/home/user space/"quoted"/back\\slash', *ARGV[2:])
    value = observe_chrome_cmdline(b'\0'.join(arg.encode() for arg in original) + b'\0',
                                  expected_arguments=original[1:])
    assert value.format == 'nul_argv' and value.arguments == original


@pytest.mark.parametrize('suffix', [b'', b'\0', b'\0\0\0'])
def test_known_chrome_executable_and_unambiguous_linux_title_are_supported(suffix):
    value = observe_chrome_cmdline(flattened() + suffix, executable=EXE,
                                  expected_arguments=ARGV[1:])
    assert value.format == 'linux_title'
    assert value.arguments == (EXE, *ARGV[1:])


@pytest.mark.parametrize('change', ['duplicate', 'wrong', 'forbidden'])
def test_observation_keeps_unambiguous_bad_switches_for_real_validation(change):
    arguments = list(ARGV)
    if change == 'duplicate':
        arguments.insert(-1, '--password-store=basic')
    elif change == 'wrong':
        arguments[4] = '--password-store=gnome'
    else:
        arguments.insert(-1, '--no-sandbox')
    observed = observe_chrome_cmdline(flattened(arguments) + b'\0', executable=EXE,
                                     expected_arguments=ARGV[1:])
    assert observed.arguments == (EXE, *arguments[1:])


@pytest.mark.parametrize('path', ['/home/user space/profile', '/home/user"quote/profile',
                                  "/home/user'quote/profile", '/home/user\\slash/profile'])
def test_flattened_profile_whose_boundaries_are_ambiguous_is_never_restored(path):
    arguments = (ARGV[0], '--user-data-dir=' + path, *ARGV[2:])
    with pytest.raises(AmbiguousCommandLine):
        observe_chrome_cmdline(flattened(arguments) + b'\0', executable=EXE,
                               expected_arguments=arguments[1:])


@pytest.mark.parametrize('executable', [None, '/usr/bin/python3', '/tmp/chrome'])
def test_flattened_title_requires_the_supported_real_chrome_executable(executable):
    with pytest.raises(AmbiguousCommandLine):
        observe_chrome_cmdline(flattened() + b'\0', executable=executable,
                               expected_arguments=ARGV[1:])


def test_matching_basename_at_an_unapproved_path_is_not_a_chrome_identity():
    with pytest.raises(AmbiguousCommandLine):
        observe_chrome_cmdline(flattened(executable='/tmp/chrome') + b'\0',
                               executable='/tmp/chrome', expected_arguments=ARGV[1:])


def test_deleted_binary_suffix_is_only_normalized_from_the_proc_executable_link():
    value = observe_chrome_cmdline(flattened() + b'\0', executable=EXE + ' (deleted)',
                                  expected_arguments=ARGV[1:])
    assert value.format == 'linux_title' and value.arguments == (EXE, *ARGV[1:])


def test_flat_title_with_nonzero_data_after_first_nul_is_ambiguous():
    with pytest.raises(AmbiguousCommandLine):
        observe_chrome_cmdline(flattened() + b'\0--no-sandbox\0', executable=EXE,
                               expected_arguments=ARGV[1:])


def test_unsupported_mixed_format_still_protects_the_evidenced_display_profile():
    raw = flattened() + b'\0unrecognized-residual\0'
    assert profile_evidence(raw, '/home/user/.config/ala/chrome-display')


def test_url_with_spaces_is_supported_only_with_real_argument_boundaries():
    arguments = (*ARGV[:-1], 'data:text/html,<p>synthetic recovery</p>')
    value = observe_chrome_cmdline(b'\0'.join(arg.encode() for arg in arguments) + b'\0',
                                  expected_arguments=arguments[1:])
    assert value.format == 'nul_argv' and value.arguments == arguments
    with pytest.raises(AmbiguousCommandLine):
        observe_chrome_cmdline(flattened(arguments) + b'\0', executable=EXE,
                               expected_arguments=arguments[1:])


def test_linux_title_prefix_must_match_the_executable_on_an_exact_boundary():
    raw = flattened().replace(EXE.encode(), (EXE + '-other').encode(), 1)
    with pytest.raises(AmbiguousCommandLine):
        observe_chrome_cmdline(raw + b'\0', executable=EXE, expected_arguments=ARGV[1:])


@pytest.mark.parametrize('raw', [b'', b'\0', b'\xff\0'])
def test_empty_or_unreadable_encoding_is_unknown(raw):
    with pytest.raises(AmbiguousCommandLine):
        observe_chrome_cmdline(raw, executable=EXE, expected_arguments=ARGV[1:])


@pytest.mark.parametrize('separator', [b'\t', b'\n', b'\r'])
def test_flattened_control_separators_are_not_guessed_as_argv(separator):
    raw = flattened().replace(b' --no-first-run', separator + b'--no-first-run')
    with pytest.raises(AmbiguousCommandLine):
        observe_chrome_cmdline(raw + b'\0', executable=EXE, expected_arguments=ARGV[1:])


def test_shell_quoted_flat_title_is_not_treated_as_original_shell_input():
    raw = flattened().replace(b'--user-data-dir=/home/user/.config/ala/chrome-display',
                              b'--user-data-dir="/home/user/.config/ala/chrome-display"')
    with pytest.raises(AmbiguousCommandLine):
        observe_chrome_cmdline(raw + b'\0', executable=EXE, expected_arguments=ARGV[1:])


def test_unknown_positional_argument_is_rejected_without_leaking_its_value():
    raw = flattened() + b' https://example.com/private-proc-marker'
    with pytest.raises(AmbiguousCommandLine) as failure:
        observe_chrome_cmdline(raw + b'\0', executable=EXE, expected_arguments=ARGV[1:])
    assert 'private-proc-marker' not in str(failure.value)
    assert 'example.com' not in str(failure.value)
