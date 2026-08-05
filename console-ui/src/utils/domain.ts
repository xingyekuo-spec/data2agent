/**
 * 领域(domain)着色:固定调色板按领域排序取色。
 * 拓扑图谱与对象关系页共用,保证同一领域在任何视图同色。
 */

export const DOMAIN_PALETTE = [
  '#2563eb', '#059669', '#d97706', '#dc2626',
  '#7c3aed', '#0891b2', '#db2777', '#65a30d',
] as const

/** 返回取色函数:按给定领域列表的排序位置取色(与图例同源)。 */
export function makeDomainColor(domains: readonly string[]): (domain: string) => string {
  return (domain: string) => {
    const i = domains.indexOf(domain)
    return DOMAIN_PALETTE[i >= 0 ? i % DOMAIN_PALETTE.length : 0]
  }
}
