# Contributing Guide

<div align="center">

English | [简体中文](CONTRIBUTING.zh_Hans.md)

</div>

Thank you for considering contributing to our project! Your efforts will make our project better.

Before you start contributing, please take a moment to read the following guidelines:

## How Can I Contribute?

### Reporting Issues

If you encounter an error in the project, please [report an issue](https://github.com/XiaoMi/xiaomi-miloco/issues/new/) on GitHub and provide detailed information about the error, including reproduction steps, debug-level logs, and the time when the error occurred.

- How to enable debug logging
  - Main service:

    The main service configuration file is located at `config/server_config.yaml`. If using Docker deployment, the configuration file will be placed in the same directory as where Docker is started on the host machine, in the subdirectory `./config/server_config.yaml`
    ```yaml
    # Modify config file settings
    server:
      log_level: "debug"
    ```
  - Inference service:

    The inference service configuration file is located at `config/ai_engine_config.yaml`. If using Docker deployment, the configuration file will be placed in the same directory as where Docker is started on the host machine, in the subdirectory `./config/ai_engine_config.yaml`
    ```yaml
    # Modify config file settings
    server:
      log_level: "debug"
    ```

- Log file paths are as follows:
  - When starting from source code, log files are in the project root directory `./log/` folder
  - When starting with Docker, log files are mounted in the same directory as where Docker is started on the host machine, in the subdirectory `./log/`

  Please upload the entire log directory.


### Contributing Code

1. Fork the repository and develop based on the latest code from the `main` branch.
2. Complete your code modifications, ensuring your code follows the project's coding standards.
3. Submit a pull request to the main repository.

## Pull Request Guidelines

Before submitting a pull request, please ensure the following requirements are met:

- Your pull request addresses a single issue or feature.
- You have tested your changes locally.
- Your code follows the project's code standards
  - python: Run [`pylint`](https://github.com/google/pyink) with this project's [pylintrc](.pylintrc) to check the code.
  - c/cpp: Run [`clang-format`](https://clang.llvm.org/docs/ClangFormat.html) with this project's [clang-format](.clang-format) to check the code.
  - js: Run [`eslint`](https://github.com/eslint/eslint) with this project's [.eslintrc](web_ui/eslint.config.js) to check the code.
- Your pull request should include a clear description and relevant context information.
- All existing tests pass, and if necessary, please add tests corresponding to the modified code.
- Any dependency changes are documented.

## Commit Message Format

```
<type>: <subject>
<BLANK LINE>
<body>
<BLANK LINE>
<footer>
```

type: The following change types are available

- feat: New feature.
- fix: Bug fix.
- docs: Documentation changes only.
- style: Formatting changes only, such as commas, indentation, spaces, etc., without changing code logic.
- refactor: Code refactoring, no new features or bug fixes.
- perf: Performance optimization.
- test: Adding or modifying test cases.
- chore: Changes to build process, or dependency libraries and tools, etc.
- revert: Version rollback.

subject: A concise title describing the summary of this commit. Use imperative mood, present tense, lowercase first letter, no period at the end.

body: Describes the detailed content of this commit and explains why these changes are needed. All change types except docs must include a body.

footer: (Optional) Associated issue.

## Naming Conventions

### Xiaomi Home Naming Conventions

- When describing "Xiaomi". Variable names can use "xiaomi" or "mi".
- When describing "Xiaomi Home". Variable names can use "mihome" or "MiHome".
- When describing "MIoT". Variable names can use "miot" or "MIoT".

### Third-Party Platform Naming Conventions

- When describing "Home Assistant", you must use "Home Assistant". Variables can use "hass" or "hass_xxx".

### Other Naming Conventions

- In documentation, when Chinese sentences contain English, if the English is not enclosed in Chinese quotation marks, there must be a space between the Chinese and English. (It's best to write code comments this way as well)

