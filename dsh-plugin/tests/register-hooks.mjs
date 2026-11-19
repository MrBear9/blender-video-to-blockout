/**
 * 注册 module hooks：
 *   node --import ./tests/register-hooks.mjs tests/unit.mjs
 */
import { register } from 'node:module';
import { pathToFileURL } from 'node:url';

// register 的第二个参数是 parentURL，传 file URL 会把路径拼错。
// 用不带参数的 register + 相对 specifier 即可。
register('./hooks.mjs', import.meta.url);
