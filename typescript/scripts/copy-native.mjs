import { copyFile, mkdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
const root = fileURLToPath(new URL('../../', import.meta.url));
const file = process.platform === 'win32' ? 'memweft_node.dll' : process.platform === 'darwin' ? 'libmemweft_node.dylib' : 'libmemweft_node.so';
const target = process.env.CARGO_TARGET_DIR ? resolve(root, process.env.CARGO_TARGET_DIR) : resolve(root, 'target');
await mkdir(resolve(root, 'typescript/native'), { recursive: true });
await copyFile(resolve(target, 'release', file), resolve(root, 'typescript/native/memweft.node'));
