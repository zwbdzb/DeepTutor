const NEXT_TYPE_INCLUDE = /^\.next[^/]*\/(?:dev\/)?types\/\*\*\/\*\.ts$/;

function rewriteTypeIncludes(contents, activeIncludes, excludedTypes = []) {
  const config = JSON.parse(contents);
  const include = Array.isArray(config.include) ? config.include : [];
  config.include = [
    ...include.filter(
      (entry) =>
        typeof entry !== "string" || !NEXT_TYPE_INCLUDE.test(entry),
    ),
    ...activeIncludes,
  ];
  if (excludedTypes.length) {
    const exclude = Array.isArray(config.exclude) ? config.exclude : [];
    config.exclude = [...new Set([...exclude, ...excludedTypes])];
  }
  return `${JSON.stringify(config, null, 2)}\n`;
}

/** Restrict generated route types to the output tree used by this build. */
export function configureTypeIncludes(contents, distDir) {
  // 只注入本次构建自己的 distDir/types。历史上这里还并入了
  // `${distDir}/dev/types/**/*.ts`，后果是：只要仓库里残留一次 `next dev`
  // 留下的 .next/dev 类型树，生产构建就会把它一起做类型检查，撞上早已删除
  // 的路由（实证：.next/dev/types/validator.ts 报 /courses 不满足
  // LayoutRoutes，next build 在 TypeScript 阶段直接失败）。dev 模式不走
  // build.mjs，这条注入没有任何正当用途，只会把陈旧类型拉进检查。
  // Explicitly exclude dev validators because Next's path filter can miss Windows paths.
  return rewriteTypeIncludes(
    contents,
    [`${distDir}/types/**/*.ts`],
    [`${distDir}/dev/types/**/*.ts`],
  );
}

/** Type-check source without trusting an arbitrary historical Next cache. */
export function removeNextTypeIncludes(contents) {
  return rewriteTypeIncludes(contents, []);
}
