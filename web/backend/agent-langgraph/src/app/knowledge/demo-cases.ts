/**
 * 内置合成案例（三条，均为合成数据，非真实患者）。
 *
 * 用于演示三种路径：
 *  - 案例1 湿疹样皮疹：资料齐全、无红旗 → 完整 RAG → 待审核草稿；
 *  - 案例2 带状疱疹红旗：资料齐全、命中红旗 → 草稿标注「升级人工审核」；
 *  - 案例3 资料不足：必填缺失 → interrupt 要求补充 → 补齐后继续。
 */

import type { CaseRecord } from "../state.js";

export interface DemoCase {
  id: "1" | "2" | "3" | "4";
  label: string;
  triggers: string[];
  case: CaseRecord;
}

/** 案例3 演示用补充回复（与 mergeCaseReply 的「字段：值」解析兼容）。 */
export const DEMO_COMPLETION_REPLY =
  "补充：病程5天；变化趋势逐渐扩大；诱因新换护肤品后出现；既往史无特殊；过敏史否认药物食物过敏；当前用药未使用药物";

export const DEMO_CASES: DemoCase[] = [
  {
    id: "1",
    label: "资料充分的常见病例",
    triggers: ["案例1", "case1", "湿疹样皮疹", "湿疹样"],
    case: {
      body_region: "双手背及肘窝（双侧对称）",
      chief_complaint: "反复红斑瘙痒3周，加重3天",
      symptoms: "对称分布红斑、丘疹，干燥脱屑，夜间瘙痒明显，无渗出",
      duration: "3周（近3天加重）",
      trend: "近3天皮损范围扩大、瘙痒加重",
      triggers: "接触洗涤剂后加重；冬季干燥",
      past_history: "特应性体质，季节性过敏性鼻炎",
      allergies: "否认药物、食物过敏史",
      current_medications: "外用保湿霜（每日2次）",
      photo_note: "合成皮肤照片（脱敏，非真实患者）：双手背对称红斑脱屑",
    },
  },
  {
    id: "2",
    label: "红旗病例",
    triggers: ["案例2", "case2", "带状疱疹", "带状疱疹红旗"],
    case: {
      body_region: "左侧胸背部（单侧）",
      chief_complaint: "左侧胸背灼痛伴成簇水疱2天",
      symptoms: "单侧簇集性水疱，沿肋间神经走向呈带状分布，灼痛、刺痛，局部皮肤触痛敏感",
      duration: "2天（疼痛先于皮疹1天出现）",
      trend: "水疱数量渐增，疼痛持续",
      triggers: "近1周劳累、睡眠差",
      past_history: "高血压病史10年，规律服药控制",
      allergies: "青霉素过敏（皮试阳性史）",
      current_medications: "口服对乙酰氨基酚（自行镇痛）",
      photo_note: "合成皮肤照片（脱敏，非真实患者）：左侧胸背单侧簇集水疱呈带状分布",
    },
  },
  {
    id: "3",
    label: "信息不足病例",
    triggers: ["案例3", "case3", "资料不足"],
    case: {
      body_region: "面部",
      chief_complaint: "面部红斑脱屑",
      symptoms: "面部片状红斑伴细小脱屑，轻度瘙痒",
      duration: "",
      trend: "",
      triggers: "",
      past_history: "",
      allergies: "",
      current_medications: "",
      photo_note: "合成皮肤照片（脱敏，非真实患者）：面部红斑脱屑",
    },
  },
  {
    id: "4",
    label: "无可靠证据病例",
    // 知识库未覆盖主题（甲部色素性病变）：演示「不足以判断、不编造」——
    // 检索 0 命中 → 草稿无鉴别诊断、标注知识库未覆盖与不确定性，建议面诊。
    triggers: ["案例4", "case4", "无答案"],
    case: {
      body_region: "右手拇指（甲床）",
      chief_complaint: "甲床纵行黑线伴甲板纵裂6个月",
      symptoms: "右手拇指甲床纵行黑线，宽约3毫米，伴甲板纵裂，无疼痛，无甲周色素沉着",
      duration: "6个月（缓慢增宽）",
      trend: "黑线逐渐增宽，甲板纵裂同前",
      triggers: "无明确诱因，无外伤史",
      past_history: "体健，无慢性病史",
      allergies: "否认药物食物过敏史",
      current_medications: "未使用药物",
      photo_note: "合成皮肤照片（脱敏，非真实患者）：右手拇指甲床纵行黑线",
    },
  },
];

/** 识别合成案例指令（如「案例1」/「case2」/「湿疹样皮疹」）。 */
export function matchDemoCase(input: string): DemoCase | null {
  const text = input.trim().toLowerCase();
  if (!text) return null;
  for (const demo of DEMO_CASES) {
    if (demo.triggers.some((t) => text === t.toLowerCase() || text.includes(t.toLowerCase()))) {
      return demo;
    }
  }
  return null;
}
