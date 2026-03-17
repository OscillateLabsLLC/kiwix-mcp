# Contributing to kiwix-mcp

Thanks for your interest in contributing!

## Development Setup

1. Clone and install:

   ```bash
   git clone https://github.com/OscillateLabsLLC/kiwix-mcp
   cd kiwix-mcp
   just install
   ```

   Or manually:

   ```bash
   pip install -e ".[dev]"
   ```

2. Run tests:

   ```bash
   just test
   ```

3. Lint and format:

   ```bash
   just lint
   just fmt
   ```

## Available `just` Commands

```bash
just          # list all commands
just install  # install with dev deps
just test     # run tests
just lint     # ruff check
just fmt      # ruff format
just build    # build package
just clean    # remove build artifacts
```

## Commits

Use [conventional commits](https://www.conventionalcommits.org/):

```text
feat: add new MCP tool
fix: handle missing book slug
docs: update client library examples
```

## Pull Requests

1. Create a feature branch: `git checkout -b feat-my-feature`
2. Make your changes and add tests
3. Run `just test` and `just lint`
4. Commit with clear messages
5. Open a pull request

## License

By contributing, you agree that your contributions will be licensed under the Apache-2.0 License.
