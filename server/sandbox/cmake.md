# C and C++ builds with CMake

CMake 4.4 (`cmake`, `ctest`, `cpack`), Ninja, GCC/G++, GNU Make, and `pkg-config`
are preinstalled for both queue tasks and bridge sessions.

- Build out of tree, outside the checkout, and prefer Ninja for new build trees:
  `cmake -S . -B /tmp/build/<project> -G Ninja -DCMAKE_BUILD_TYPE=Debug`, then
  `cmake --build /tmp/build/<project>` and
  `ctest --test-dir /tmp/build/<project> --output-on-failure`. When the project has
  `CMakePresets.json`, use its presets (`cmake --list-presets`, `cmake --preset <name>`,
  `ctest --preset <name>`).
- Build trees are disposable output. Untracked directories that contain a
  `CMakeCache.txt` are left out of synced and auto-committed results, but never
  commit build trees, `compile_commands.json`, or downloaded dependencies yourself.
- CMake 4 rejects projects whose `cmake_minimum_required` is below 3.5. To build such
  a project anyway, configure with `-DCMAKE_POLICY_VERSION_MINIMUM=3.5` and say so in
  your report; change the project's minimum version only when the task asks for it.
- Missing libraries: install the Debian development package
  (`apt-get install -y libfoo-dev`) and name it in your report. Packages you install
  are not part of the repository.
- Makefile builds run one job at a time unless asked otherwise: pass `--parallel`
  with a job count, such as `cmake --build <dir> --parallel 3`. The sandbox has a fixed
  memory limit; if the compiler is killed, rebuild with fewer jobs.
