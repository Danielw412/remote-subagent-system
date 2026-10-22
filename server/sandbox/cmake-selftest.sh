#!/usr/bin/env bash
# Offline smoke test of the CMake toolchain: configure, build, and test a small C/C++ project
# with the Ninja and Makefile generators, then check that a build tree configured inside a git
# checkout is kept out of results by rsa-ignore-build-trees. Needs no model or network.
set -euo pipefail

scratch=$(mktemp -d /tmp/rsa-cmake-selftest-XXXXXX)
trap 'rm -rf "$scratch"' EXIT
src=$scratch/src
mkdir -p "$src/tools"
cat >"$src/CMakeLists.txt" <<'EOF'
cmake_minimum_required(VERSION 3.25)
project(rsa_fixture LANGUAGES C CXX)
enable_testing()
add_library(answer STATIC answer.c)
add_executable(fixture fixture.cpp)
target_link_libraries(fixture PRIVATE answer)
target_compile_features(fixture PRIVATE cxx_std_20)
add_test(NAME fixture COMMAND fixture)
EOF
printf 'int rsa_answer(void) { return 1729; }\n' >"$src/answer.c"
cat >"$src/fixture.cpp" <<'EOF'
#include <span>
extern "C" int rsa_answer(void);
int main() {
  int values[] = {rsa_answer()};
  return std::span(values).front() == 1729 ? 0 : 1;
}
EOF
printf '# tracked\n' >"$src/tools/README"

for gen in Ninja "Unix Makefiles"; do
  build="$scratch/build-${gen// /-}"
  cmake -S "$src" -B "$build" -G "$gen" -DCMAKE_BUILD_TYPE=Release
  cmake --build "$build" --parallel 2
  ctest --test-dir "$build" --output-on-failure
done

# Inside a checkout: a pure build tree is excluded; a directory with tracked files is not.
cd "$src"
git init -q
git add -A
git -c user.name=rsa -c user.email=rsa@localhost commit -qm fixture
cmake -S . -B build -G Ninja
cmake --build build
touch tools/CMakeCache.txt
rsa-ignore-build-trees
git add -A
staged=$(git diff --cached --name-only)
[ "$staged" = tools/CMakeCache.txt ] || { echo "unexpected staged files: $staged" >&2; exit 1; }
git clean -fdq
[ -f build/CMakeCache.txt ] || { echo "excluded build tree did not survive git clean" >&2; exit 1; }

echo "PASS $(cmake --version | head -n1), ninja $(ninja --version)"
