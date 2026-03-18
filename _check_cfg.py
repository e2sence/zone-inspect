import inspection_config as cfg

for z in [0.0, 0.5, 1.0, 1.5, 2.0]:
    cfg.apply_zone_sensitivity(z)
    print(f"Zone={z:.1f}: SIM_THR={cfg.PATCH_SIM_THRESHOLD} DEF_W={cfg.PATCH_DEFECT_WEIGHT} "
          f"HI_DISC={cfg.PATCH_HIGH_SIM_DISCOUNT} OK_THR={cfg.VERDICT_OK_THRESHOLD} "
          f"WARN_THR={cfg.VERDICT_WARN_THRESHOLD} SAFETY_SSIM={cfg.SAFETY_SSIM_LOW} "
          f"SAFETY_SIM={cfg.SAFETY_SIM_LOW}")
print()
for s in [0.0, 0.51, 1.0, 1.5, 2.0]:
    cfg.apply_subzone_sensitivity(s)
    print(f"Sub={s:.2f}: SIM_THR={cfg.SUBZONE_PATCH_SIM_THRESHOLD} OK_THR={cfg.SUBZONE_VERDICT_OK_THRESHOLD} "
          f"WARN_THR={cfg.SUBZONE_VERDICT_WARN_THRESHOLD} DISC_GOOD={cfg.SUBZONE_DISCOUNT_GOOD} "
          f"DISC_MIN={cfg.SUBZONE_DISCOUNT_MINIMAL} FORCED={cfg.SUBZONE_FORCED_DEFECT_PCT}") 
