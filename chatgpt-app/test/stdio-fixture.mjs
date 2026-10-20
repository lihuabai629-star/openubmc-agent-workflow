import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { ListToolsRequestSchema, CallToolRequestSchema } from "@modelcontextprotocol/sdk/types.js";
const names = ["observe", "execute", "runtime_status", "case_read", "evidence_attach", "openubmc_setup_prepare", "openubmc_kb_query"];
const server = new Server({ name: "stdio-fixture", version: "1" }, { capabilities: { tools: {} } });
server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: names.map(name => ({ name, inputSchema: { type: "object" } })) }));
server.setRequestHandler(CallToolRequestSchema, async request => ({ content: [], structuredContent: { arguments: request.params.arguments, metadata: request.params._meta } }));
await server.connect(new StdioServerTransport());
