const readline = require('node:readline');
const { randomUUID } = require('node:crypto');
const { resolve, isAbsolute } = require('node:path');
const { MyOutBrainHarness } = require('./myoutbrain-harness');

const HARNESS_OPTIONS = {
  memoryRoot: resolve(process.env.MYOUTBRAIN_MEMORY_ROOT ?? process.env.MYOUTBRAIN_ROOT ?? `${process.cwd()}${require('node:path').sep}.myoutbrain`),
  pythonExecutable: process.env.MYOUTBRAIN_PYTHON_EXECUTABLE || 'python',
  startupTimeoutMs: Number(process.env.MYOUTBRAIN_STARTUP_TIMEOUT_MS) || 10_000,
  mcpCommand: parseMcpCommand(process.env.MYOUTBRAIN_MCP_COMMAND),
  environment: {
    ASTERFLOW_MYOUTBRAIN_MCP_COMMAND: process.env.ASTERFLOW_MYOUTBRAIN_MCP_COMMAND,
    MYOUTBRAIN_MEMORY_ROOT: process.env.MYOUTBRAIN_MEMORY_ROOT,
    MYOUTBRAIN_ROOT: process.env.MYOUTBRAIN_ROOT,
  },
};
if (process.env.MYOUTBRAIN_PACKAGE_ROOT) {
  HARNESS_OPTIONS.packageRoot = process.env.MYOUTBRAIN_PACKAGE_ROOT;
}

const harness = new MyOutBrainHarness(HARNESS_OPTIONS);

const INPUT_SCHEMAS = {
  memory_gateway: {
    type: 'object',
    additionalProperties: false,
    required: ['request'],
    properties: {
      request: {
        type: 'object',
        additionalProperties: false,
        required: ['protocol', 'client', 'operation', 'parameters'],
        properties: {
          protocol: {
            type: 'object',
            additionalProperties: false,
            required: ['major', 'minor'],
            properties: {
              major: { const: 3 },
              minor: { const: 0 },
            },
          },
          client: {
            type: 'object',
            additionalProperties: false,
            required: ['name', 'capabilities'],
            properties: {
              name: { type: 'string' },
              capabilities: { type: 'array', items: { type: 'string' }, uniqueItems: true },
            },
          },
          operation: { type: 'string' },
          parameters: { type: 'object' },
          idempotency_key: { type: 'string' },
        },
      },
    },
  },
  memory_decide_card: {
    type: 'object',
    additionalProperties: false,
    required: ['cardId', 'decision', 'explicitUserDecision'],
    properties: {
      cardId: { type: 'string' },
      decision: { type: 'string', enum: ['accept', 'reject', 'defer'] },
      explicitUserDecision: { type: 'boolean', const: true },
    },
  },
  memory_split_card: {
    type: 'object',
    additionalProperties: false,
    required: ['cardId', 'cards', 'explicitUserDecision'],
    properties: {
      cardId: { type: 'string' },
      cards: {
        type: 'array',
        minItems: 2,
        maxItems: 12,
        items: {
          type: 'object',
          additionalProperties: false,
          required: ['problem', 'claim', 'evidence'],
          properties: {
            problem: { type: 'string' },
            claim: { type: 'string' },
            evidence: { type: 'array', items: { type: 'string' } },
            uncertainty: { type: 'array', items: { type: 'string' } },
          },
        },
      },
      explicitUserDecision: { type: 'boolean', const: true },
    },
  },
  memory_decide_review: {
    type: 'object',
    additionalProperties: false,
    required: ['proposalId', 'decision', 'explicitUserDecision'],
    properties: {
      proposalId: { type: 'string' },
      decision: { type: 'string', enum: ['accept', 'reject', 'defer'] },
      explicitUserDecision: { type: 'boolean', const: true },
    },
  },
};

function parseMcpCommand(raw) {
  if (!raw || typeof raw !== 'string') return [];
  const trimmed = raw.trim();
  if (!trimmed) return [];
  try {
    const parsed = JSON.parse(trimmed);
    if (Array.isArray(parsed)) return parsed.filter((value) => typeof value === 'string');
  } catch (_error) {
    // fallback below
  }
  const tokens = trimmed.match(/(?:[^\s"']+|"[^"]*"|'[^']*')+/g);
  return (tokens ?? []).map((token) => {
    if ((token.startsWith('"') && token.endsWith('"')) || (token.startsWith("'") && token.endsWith("'"))) {
      return token.slice(1, -1);
    }
    return token;
  });
}

function extractArgs(message) {
  if (!message || typeof message !== 'object') return {};
  if (message.arguments && typeof message.arguments === 'object') return message.arguments;
  if (message.input && typeof message.input === 'object') return message.input;
  return {};
}

function ensureObject(value, field) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${field} must be an object.`);
  }
  return value;
}

function explicitDecisionGuard(input) {
  if (input.explicitUserDecision !== true) {
    throw new Error('该操作需要 explicitUserDecision = true。');
  }
}

function responseForTool(result) {
  return {
    content: [{ type: 'text', text: JSON.stringify(result) }],
    structuredContent: result,
  };
}

async function handleCall(name, args) {
  if (name === 'myoutbrain_gateway') {
    const request = ensureObject(args, 'args').request;
    if (!request) throw new Error('request is required.');
    return harness.callGateway(request);
  }
  if (name === 'memory_list_collector_cards') {
    const cards = await harness.listCollectorCards();
    return responseForTool({ cards });
  }
  if (name === 'memory_split_collector_card') {
    explicitDecisionGuard(args);
    return harness.splitCollectorCard(
      String(args.cardId),
      (args.cards ?? []).map((card) => ({
        problem: String(card.problem || ''),
        claim: String(card.claim || ''),
        evidence: Array.isArray(card.evidence) ? card.evidence.map(String) : [],
        uncertainty: Array.isArray(card.uncertainty) ? card.uncertainty.map(String) : [],
      })),
      'user',
      randomUUID(),
    );
  }
  if (name === 'memory_decide_collector_card') {
    explicitDecisionGuard(args);
    const decision = String(args.decision);
    if (!['accept', 'reject', 'defer'].includes(decision)) {
      throw new Error('decision 必须为 accept / reject / defer。');
    }
    return harness.decideCollectorCard(
      String(args.cardId),
      decision,
      'user',
      randomUUID(),
    );
  }
  if (name === 'memory_list_review_proposals') {
    const proposals = await harness.listReviewProposals();
    return responseForTool({ proposals });
  }
  if (name === 'memory_decide_review_proposal') {
    explicitDecisionGuard(args);
    const decision = String(args.decision);
    if (!['accept', 'reject', 'defer'].includes(decision)) {
      throw new Error('decision 必须为 accept / reject / defer。');
    }
    return harness.decideReviewProposal(
      String(args.proposalId),
      decision,
      'user',
      randomUUID(),
    );
  }
  throw new Error(`Unknown tool: ${name}`);
}

function toolList() {
  return {
    tools: [
      {
        name: 'myoutbrain_gateway',
        description: 'Invoke MyOutBrain Memory Domain 3.0 operations directly.',
        inputSchema: INPUT_SCHEMAS.memory_gateway,
      },
      {
        name: 'memory_list_collector_cards',
        description: 'List pending and deferred Temporary Knowledge Cards.',
        inputSchema: { type: 'object', additionalProperties: false, properties: {} },
      },
      {
        name: 'memory_split_collector_card',
        description: 'Split one pending/deferred collector card into multiple clean cards.',
        inputSchema: INPUT_SCHEMAS.memory_split_card,
      },
      {
        name: 'memory_decide_collector_card',
        description: 'Apply explicit creator decision to one collector card (accept/reject/defer).',
        inputSchema: INPUT_SCHEMAS.memory_decide_card,
      },
      {
        name: 'memory_list_review_proposals',
        description: 'List review proposals from memory inspect.',
        inputSchema: { type: 'object', additionalProperties: false, properties: {} },
      },
      {
        name: 'memory_decide_review_proposal',
        description: 'Resolve a review proposal with explicit decision.',
        inputSchema: INPUT_SCHEMAS.memory_decide_review,
      },
    ],
  };
}

const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
rl.on('line', async (line) => {
  let message;
  try {
    message = JSON.parse(line);
  } catch (_error) {
    return;
  }
  if (!message || typeof message !== 'object' || message.jsonrpc !== '2.0') return;
  if (typeof message.id === 'undefined') return;

  const id = message.id;
  try {
    if (message.method === 'initialize') {
      respond(id, {
        jsonrpc: '2.0',
        id,
        result: {
          protocolVersion: '2025-11-25',
          capabilities: { tools: {} },
          serverInfo: {
            name: 'myoutbrain-memory-plugin-server',
            version: '0.1.0',
            description: 'MyOutBrain memory harness MCP server.',
          },
          instructions: (
            'Use memory tools in this plugin to call MyOutBrain Memory Authority operations.'
          ),
        },
      });
      return;
    }
    if (message.method === 'tools/list') {
      respond(id, { jsonrpc: '2.0', id, result: toolList() });
      return;
    }
    if (message.method === 'tools/call') {
      const { name } = ensureObject(message.params, 'params');
      const args = extractArgs(message.params);
      const result = await handleCall(name, args);
      const payload = result.structuredContent === undefined
        ? responseForTool(result)
        : result;
      respond(id, { jsonrpc: '2.0', id, result: payload });
      return;
    }
    respond(id, {
      jsonrpc: '2.0',
      id,
      error: { code: -32601, message: `Method not found: ${message.method}` },
    });
  } catch (error) {
    respond(id, {
      jsonrpc: '2.0',
      id,
      error: {
        code: -32603,
        message: error instanceof Error ? error.message : String(error),
      },
    });
  }
});

function respond(id, payload) {
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

process.on('beforeExit', () => {
  void harness.close();
});
process.on('SIGINT', async () => {
  await harness.close();
  process.exit(0);
});
