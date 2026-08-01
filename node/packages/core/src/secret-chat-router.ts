import {
  isDirectSecretChatIntent,
  routeOwnerSecretChat as routeStrictOwnerSecretChat,
  type DirectSecretChatClient,
  type DirectSecretRouteResult
} from "./secret-chat-direct.ts";

const ACTION_BOUNDARY = /((?:删除|移除|去掉|停用)[^\n，,。；;]{1,80})[，,](?=\s*(?:把|将)?\s*[^\n，,。；;]{1,80}(?:改成|改为|更新为|替换为|设置为|设为|=|:|：))/gi;

export function normalizeOwnerSecretActionClauses(text: string): string {
  let current = String(text ?? "");
  for (let index = 0; index < 8; index += 1) {
    const next = current.replace(ACTION_BOUNDARY, "$1；");
    if (next === current) break;
    current = next;
  }
  return current;
}

export { isDirectSecretChatIntent };
export type { DirectSecretChatClient, DirectSecretRouteResult };

export async function routeOwnerSecretChat(
  text: string,
  client: DirectSecretChatClient
): Promise<DirectSecretRouteResult> {
  return routeStrictOwnerSecretChat(normalizeOwnerSecretActionClauses(text), client);
}
