/**
 * Shared harness that treats upstream MyOutBrain MCP (stdio JSON-RPC) as a
 * transport seam for both Codex MCP and OpenCode tool adapters.
 */

const { existsSync, readFileSync } = require('node:fs');
const { spawn } = require('node:child_process');
const { mkdirSync } = require('node:fs');
const { randomUUID } = require('node:crypto');
const { dirname, resolve, isAbsolute, join, sep } = require('node:path');

const DEFAULT_PROTOCOL = { major: 3, minor: 0 };
const DEFAULT_CLIENT = {
  name: 'myoutbrain-harness',
  capabilities: ['memory-graph.v3', 'knowledge-collector.v1'],
};

function toStringArray(value) {
  if (!Array.isArray(value)) return [];
  return value
    .filter((entry) => typeof entry === 'string')
    .map((entry) => entry.trim())
    .filter(Boolean);
}

function parseMcpCommand(raw) {
  if (!raw || typeof raw !== 'string') return [];
  const trimmed = raw.trim();
  if (!trimmed) return [];
  try {
    const parsed = JSON.parse(trimmed);
    if (Array.isArray(parsed)) {
      return toStringArray(parsed);
    }
  } catch (_error) {
    // Fallback to shell-like tokenization.
  }
  const tokens = trimmed.match(/(?:[^\s"']+|"[^"]*"|'[^']*')+/g);
  return (tokens ?? []).map((token) => {
    if ((token.startsWith('"') && token.endsWith('"')) || (token.startsWith("'") && token.endsWith("'"))) {
      return token.slice(1, -1);
    }
    return token;
  }).filter(Boolean);
}

function safeStringify(value) {
  if (typeof value === 'string') return value;
  return JSON.stringify(value);
}

function parseResponsePayload(response) {
  if (!response || typeof response !== 'object') {
    throw new Error('Invalid response from MyOutBrain MCP.');
  }
  if (response.error) {
    const code = response.error?.code;
    const message = response.error?.message ?? 'unknown MyOutBrain MCP error';
    throw new Error(`MyOutBrain MCP error (${code ?? 'unknown'}): ${message}`);
  }
  const structured = response.result?.structuredContent;
  if (structured && typeof structured === 'object') {
    const protocol = structured.protocol;
    if (protocol && (protocol.major !== 3 || protocol.minor !== 0)) {
      throw new Error(`Unsupported MyOutBrain protocol ${protocol.major ?? '?'}.${protocol.minor ?? '?'}.`);
    }
    if ('ok' in structured && structured.ok === false) {
      const category = structured.error?.category || 'memory-error';
      const message = structured.error?.message || 'MyOutBrain rejected request.';
      throw new Error(`${category}: ${message}`);
    }
    if ('result' in structured) return structured.result;
  }
  return response.result?.structuredContent ?? response.result;
}

class MyOutBrainHarness {
  #options;
  #process;
  #pending = new Map();
  #startPromise;
  #nextId = 1;
  #stderrTail = '';
  #buffer = '';

  constructor(options = {}) {
    this.#options = {
      startupTimeoutMs: 10_000,
      pythonExecutable: options.pythonExecutable || 'python',
      ...options,
      packageRoot: options.packageRoot ? resolve(options.packageRoot) : undefined,
      memoryRoot: options.memoryRoot ? resolve(options.memoryRoot) : process.cwd(),
    };
    if (!this.#options.memoryRoot.trim()) {
      this.#options.memoryRoot = process.cwd();
    }
  }

  get memoryRoot() {
    return this.#options.memoryRoot;
  }

  async start() {
    if (this.#process) return;
    if (this.#startPromise) return this.#startPromise;
    this.#startPromise = (async () => {
      const command = this.resolveCommand();
      const cwd = this.#options.packageRoot
        ? this.#options.packageRoot
        : dirname(__filename);
      const pyPath = this.resolvePythonPath();
      const env = {
        ...process.env,
        ...(this.#options.environment ?? {}),
        PYTHONUTF8: '1',
        PYTHONIOENCODING: 'utf-8',
        MYOUTBRAIN_SKIP_MODEL_PREPARATION: '1',
      };
      if (pyPath) env.PYTHONPATH = `${pyPath}${sep}${process.env.PYTHONPATH || ''}`;

      const child = spawn(command[0], command.slice(1), {
        cwd,
        env,
        stdio: ['pipe', 'pipe', 'pipe'],
      });
      this.#process = child;
      child.stdout.on('data', (chunk) => this.#onStdout(chunk));
      child.stderr.on('data', (chunk) => this.#onStderr(chunk));
      const closeReject = (error) => {
        this.#rejectAll(error);
      };
      child.once('close', (code) => {
        const reason = `MyOutBrain MCP exited with code ${code}. ${this.#stderrTail ? `stderr: ${this.#stderrTail}` : ''}`.trim();
        if (this.#process === child) {
          this.#process = undefined;
        }
        closeReject(new Error(reason));
      });
      child.once('error', (error) => {
        if (this.#process === child) this.#process = undefined;
        this.#rejectAll(error);
      });

      const timeout = new Promise((_, reject) => {
        setTimeout(() => reject(new Error(`MyOutBrain MCP startup timed out after ${this.#options.startupTimeoutMs} ms.`)), this.#options.startupTimeoutMs).unref?.();
      });
      const init = this.#rpc('initialize', {
        protocolVersion: '2025-11-25',
        capabilities: {},
        clientInfo: { name: 'myoutbrain-harness', version: '0.1.0' },
      });
      await Promise.race([init, timeout]);
    })().catch((error) => {
      this.#startPromise = undefined;
      throw error;
    });
    try {
      await this.#startPromise;
    } finally {
      this.#startPromise = undefined;
    }
  }

  close() {
    const processHandle = this.#process;
    if (!processHandle) return Promise.resolve();
    return new Promise((resolve) => {
      this.#process = undefined;
      this.#rejectAll(new Error('MyOutBrain harness closed.'));
      processHandle.once('close', () => resolve());
      if (!processHandle.killed) processHandle.kill();
    });
  }

  resolveCommand() {
    const explicit = toStringArray(this.#options.mcpCommand) || [];
    if (explicit.length) return this.interpolateCommand(explicit);
    const envCommand = process.env.MYOUTBRAIN_MCP_COMMAND
      ? parseMcpCommand(process.env.MYOUTBRAIN_MCP_COMMAND)
      : [];
    if (envCommand.length) return this.interpolateCommand(envCommand);
    return this.interpolateCommand([
      this.#options.pythonExecutable,
      '-m',
      'myoutbrain',
      'mcp',
      '--root',
      this.#options.memoryRoot,
    ]);
  }

  resolvePythonPath() {
    const explicit = this.#options.pythonPath;
    if (explicit && existsSync(explicit)) return explicit;
    const packageRoot = this.#options.packageRoot;
    if (packageRoot && existsSync(packageRoot)) {
      const candidate = join(packageRoot, 'src');
      if (existsSync(candidate)) return candidate;
    }
    const defaultAsterflow = resolve(dirname(__filename), '..', '..', 'asterflow', 'upstreams', 'myoutbrain', 'src');
    if (existsSync(defaultAsterflow)) return defaultAsterflow;
    const memoryPackage = resolve(dirname(__filename), '..', 'asterflow', 'upstreams', 'myoutbrain', 'src');
    if (existsSync(memoryPackage)) return memoryPackage;
    return undefined;
  }

  interpolateCommand(command) {
    const root = this.#options.memoryRoot;
    return command.map((token) => (
      token
        .replaceAll('{memoryRoot}', root)
        .replaceAll('{root}', root)
        .replaceAll('$memoryRoot', root)
        .replaceAll('$root', root)
        .replaceAll('${memoryRoot}', root)
        .replaceAll('${root}', root)
    ));
  }

  async callGateway(request) {
    await this.start();
    const response = await this.#rpc('tools/call', {
      name: 'myoutbrain_gateway',
      arguments: { request },
    });
    return parseResponsePayload(response);
  }

  async callOperation(operation, parameters = {}, idempotencyKey) {
    const request = {
      protocol: { ...DEFAULT_PROTOCOL },
      client: { ...DEFAULT_CLIENT },
      operation,
      parameters: { ...(parameters || {}) },
      ...(idempotencyKey ? { idempotency_key: idempotencyKey } : {}),
    };
    return this.callGateway(request);
  }

  async listCollectorCards(statuses = ['pending', 'deferred']) {
    const value = await this.callOperation('collector.list', {
      ...(statuses.length ? { statuses } : {}),
    });
    return value.cards ?? [];
  }

  async splitCollectorCard(cardId, cards, decidedBy = 'user', idempotencyKey) {
    return this.callOperation('collector.split', {
      card_id: cardId,
      cards: cards.map((card) => ({
        problem: card.problem,
        claim: card.claim,
        evidence: card.evidence ?? [],
        uncertainty: card.uncertainty ?? [],
      })),
      decided_by: decidedBy,
    }, idempotencyKey);
  }

  async decideCollectorCard(cardId, decision, decidedBy = 'user', idempotencyKey) {
    if (decision === 'accept') {
      return this.callOperation('collector.commit', {
        card_id: cardId,
        decided_by: decidedBy,
      }, idempotencyKey);
    }
    return this.callOperation('collector.decide', {
      card_id: cardId,
      decision,
      decided_by: decidedBy,
    }, idempotencyKey);
  }

  async listReviewProposals() {
    const inspection = await this.callOperation('memory.inspect', {});
    return inspection.review_proposals ?? [];
  }

  async decideReviewProposal(proposalId, decision, decidedBy = 'user', idempotencyKey) {
    return this.callOperation('review.decide', {
      proposal_id: proposalId,
      decision,
      decided_by: decidedBy,
    }, idempotencyKey);
  }

  #onStdout(chunk) {
    if (!this.#process) return;
    this.#buffer += chunk.toString('utf8');
    let index = this.#buffer.indexOf('\n');
    while (index >= 0) {
      const line = this.#buffer.slice(0, index).trim();
      this.#buffer = this.#buffer.slice(index + 1);
      if (line) {
        try {
          const response = JSON.parse(line);
          const id = response?.id != null ? String(response.id) : undefined;
          if (!id) {
            index = this.#buffer.indexOf('\n');
            continue;
          }
          const pending = this.#pending.get(id);
          if (!pending) {
            index = this.#buffer.indexOf('\n');
            continue;
          }
          this.#pending.delete(id);
          pending.resolve(response);
        } catch (error) {
          this.#rejectAll(error instanceof Error ? error : new Error(String(error)));
        }
      }
      index = this.#buffer.indexOf('\n');
    }
  }

  #onStderr(chunk) {
    const text = chunk.toString('utf8');
    if (!text) return;
    this.#stderrTail = `${this.#stderrTail}\n${text}`.slice(-4000);
  }

  #rejectAll(error) {
    for (const pending of this.#pending.values()) {
      pending.reject(error);
    }
    this.#pending.clear();
  }

  #rpc(method, params) {
    const processHandle = this.#process;
    if (!processHandle) throw new Error('MyOutBrain process is not running.');
    const id = String(this.#nextId++);
    return new Promise((resolve, reject) => {
      this.#pending.set(id, { resolve, reject });
      const message = safeStringify({ jsonrpc: '2.0', id, method, params });
      processHandle.stdin.write(`${message}\n`);
    });
  }
}

function createRequest(operation, parameters = {}, idempotencyKey) {
  return {
    protocol: { ...DEFAULT_PROTOCOL },
    client: { ...DEFAULT_CLIENT },
    operation,
    parameters,
    ...(idempotencyKey ? { idempotency_key: idempotencyKey } : {}),
  };
}

module.exports = {
  MyOutBrainHarness,
  createRequest,
  DEFAULT_PROTOCOL,
  DEFAULT_CLIENT,
  randomUUID,
  parseMcpCommand,
};
