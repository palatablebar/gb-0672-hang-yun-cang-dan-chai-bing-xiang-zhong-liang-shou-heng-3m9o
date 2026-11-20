# 舱单数据定义

id是主单、分组单或箱货明细唯一字符串编号，parent为空表示主单。节点申报件数、净重和毛重是其完整子树的申报总量，校验时只加直接子节点，避免多层重复计数。包装carton=1件，pallet=10件；重量kg与t统一为kg。件数必须非负整数，净重非负且毛重不得低于净重。
拆并结构采用每节点最多一个父级的有向图；自环、长环、缺父级均阻断封存，保留环路径。不会以平面求和替代逐层校验。
危险品按明确分类与UN编号检查，3类与5.1类禁止同箱；未知分类和缺失编号需复核。此演示规则只覆盖文中三类，不替代运输合规审批。
报关总量只汇总主单，与context.json内的申报件数、净重和毛重逐项比较。重复同值折叠，重复编号冲突全部隔离。原始计数=采纳+隔离+空行+重复表头+重复同值行。
封存需所有图、重量、危险品和采信问题处理完毕。数值均Decimal，对外字符串保留精度，编号不转整数。

## 拆并预演裁定（rehearsal）

指令 instruction 由 limits（capacity_pieces 正整数、gross_kg 正数字，默认 30 件 / 400kg）与 items 组成；每个 item 含 master_id、plan（hold 整票不动 / split 按箱容拆层）及可选单票箱容覆盖。同一指令重复提交按 sha256 指纹幂等，不新增第二套裁定。

每张主单裁定 verdict：conservation.rows 给出件数/净重/毛重逐层守恒差异；groups 给出 hold 单箱或 split 首次适应递减分箱结果及 violations（OVER_CAPACITY/OVER_GROSS）；capacity_and_weight_hold 仅在守恒成立且所有箱组同时满足件数与毛重上限时为真。findings 分 blocker/supplement/info 三级，汇总为 READY/SUPPLEMENT/BLOCKED。split 产生多箱组时记 SPLIT_DECLARATION（改变上级申报来源，须变更申报）；单箱可容纳记 SPLIT_NOT_NEEDED 信息项。

正式应用不覆盖原批次：派生 run_id 以 AP- 开头，manifest.derived_from 指向原批次，拆层仅改写子树 container 并将 revision 加一，主单根节点保留原箱。applications 与目标批次在同一 SQLite 事务提交，失败回滚；revision 乐观锁陈旧返回 409，已应用预演重复执行返回 409。预演过程只读 state.records，不修改正式舱单。
