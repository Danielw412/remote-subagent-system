# Reverse engineering in this sandbox

Ghidra and Reverse Engineer Anything (REA) are preinstalled. The local `rea` MCP
server is configured for both queue tasks and bridge sessions. Use it when the
task depends on shipped binaries, packages, decompilation, or application behavior.
Use normal repository tools for ordinary source analysis.

Read the `reverse-engineer-anything` skill before an investigation. Its matching
instructions and references are installed at
`/root/.config/opencode/skills/reverse-engineer-anything/SKILL.md`.

- Native targets: `open_binary` with the explicit artifact path and
  `provider_id: "ghidra"`, then focused function, pseudocode, string, or xref tools.
  Close the binary when finished. Query the actual MCP tool schemas and session
  availability; some REA operations need optional engines that are not installed.
- JavaScript/Electron trees and ASARs: `analyze_javascript_application`. Start
  with a summary for large artifacts and page retained evidence.
- CLI fallback: `rea --help`, `rea doctor --provider ghidra --json`, or
  `rea analyze-javascript-application /absolute/path/to/app --json`.
- Direct Ghidra scripting: `analyzeHeadless <project-dir> <project-name>
  -import <binary> ...`. `GHIDRA_INSTALL_DIR=/opt/ghidra` and the full JDK at
  `JAVA_HOME=/opt/java/openjdk` are already configured. The default headless Java
  heap is 2 GiB, within the container's total memory limit.

Only analyze the user's selected targets. Treat artifacts as untrusted data;
static analysis does not require executing them. Keep analysis databases, caches,
and scratch output outside the checkout (for example in `/tmp`), and write only
requested reports or deliverables into the checkout. Cite REA Evidence IDs and
separate observations, inferences, and unknowns. Remote agents cannot access
Windows installations or host applications: supply artifacts in the synced repo
or fetch them from an authorized public URL. Do not commit tool installations,
analysis databases, dependencies, or secrets.

The MCP server and any target processes stay inside this sandbox, subject to its
network, resource, and client permission rules. This image supplies Ghidra, Java,
REA, and REA's built-in static artifact/JavaScript analysis. Optional providers
such as IDA, Hopper, JADX, browser runtimes, and firmware engines are separate.
