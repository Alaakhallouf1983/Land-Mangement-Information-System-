# -*- coding: utf-8 -*-
"""
=============================================================================
 Land Management Information System (LMIS) v3.0
 نظام معلومات إدارة الأراضي — الإصدار الثالث
=============================================================================
 ArcGIS Pro Python Toolbox
 Supports: Raster (Primary) + Vector (Optional)
 Based on original work by Dr. Younis Idris (ArcView 3.2)

 New in v3.0:
   - Full Raster support (Spatial Analyst Map Algebra)
   - Direct input from SoilGrids, SRTM, WorldClim, CHIRPS
   - Output as classified Raster maps
   - Vector mode preserved as optional
   - Unified Raster/Vector workflow
=============================================================================
"""

import arcpy
import os
import sys
import json
import math
import datetime

arcpy.env.overwriteOutput = True

# Safely import Spatial Analyst
try:
    from arcpy.sa import *
    SA_AVAILABLE = True
except ImportError:
    SA_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────
def _load_crops():
    base = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(base, "config", "crop_params.json")
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _crop_list():
    cp = _load_crops()
    return ["{} ({})".format(v["name_ar"], k) for k, v in cp["crops"].items()]


def _crop_key(selection):
    return selection.split("(")[-1].replace(")", "").strip()


def _check_sa(messages):
    if not SA_AVAILABLE:
        messages.addErrorMessage(
            "Spatial Analyst غير متاح. الرجاء تفعيل ترخيص Spatial Analyst في ArcGIS Pro.")
        return False
    try:
        arcpy.CheckOutExtension("Spatial")
    except Exception as e:
        messages.addErrorMessage("تعذّر تفعيل Spatial Analyst: {}".format(str(e)))
        return False
    return True


def _score_raster(raster, min_val, max_val, mode="range"):
    """
    Convert a raster to 0-100 score based on FAO limitation approach.
    mode: 'range' (optimal inside min-max), 'max' (lower is better)
    """
    if mode == "range":
        mid = (min_val + max_val) / 2.0
        half = (max_val - min_val) / 2.0
        if half == 0:
            return Con(IsNull(raster), 50, 100)
        raw = 100 - (Abs(raster - mid) / half * 100)
        return Con(IsNull(raster), 50, Con(raw < 0, 0, Con(raw > 100, 100, raw)))
    elif mode == "max":
        raw = 100 - ((raster - min_val) / float(max_val - min_val) * 100)
        return Con(IsNull(raster), 50, Con(raw < 0, 0, Con(raw > 100, 100, raw)))
    return raster


def _classify_raster(score_raster):
    """Reclassify score 0-100 → suitability class integer 1-5"""
    # 1=S1(≥80), 2=S2(60-79), 3=S3(40-59), 4=N1(20-39), 5=N2(<20)
    remap = RemapRange([[0,19.99,5],[20,39.99,4],[40,59.99,3],[60,79.99,2],[80,100,1]])
    return Reclassify(score_raster, "Value", remap)


def _suit_label(val):
    m = {1:"S1 - مناسب جداً", 2:"S2 - مناسب", 3:"S3 - هامشي",
         4:"N1 - غير مناسب حالياً", 5:"N2 - غير مناسب دائماً"}
    return m.get(val, "N/A")


def _header(tool, area=""):
    now = datetime.datetime.now()
    return ("\n" + "="*65 +
            "\n نظام معلومات إدارة الأراضي — LMIS v3.0" +
            "\n Raster & Vector Edition" +
            "\n الأداة : {}".format(tool) +
            "\n المنطقة: {}".format(area) +
            "\n التاريخ: {}".format(now.strftime("%Y-%m-%d %H:%M")) +
            "\n" + "="*65)


# =============================================================================
class Toolbox:
    def __init__(self):
        self.label = "LMIS v3.0 — نظام معلومات إدارة الأراضي"
        self.alias = "LMIS3"
        self.description = (
            "LMIS v3.0 — يدعم Raster و Vector\n"
            "14 أداة تحليلية: ملاءمة، إنتاجية، جودة تربة، ري، تدهور، اقتصاد، بيئة"
        )
        self.tools = [
            RasterCropSuitability,
            RasterProductionCapacity,
            RasterSoilQualityIndex,
            RasterIrrigationSuitability,
            RasterLandDegradation,
            RasterErosionRUSLE,
            RasterSOCStock,
            RasterMultiCriteria,
            RasterEconomicEval,
            RasterLandUseOptimize,
            RasterEnvironmentalImpact,
            RasterLPI,
            VectorSuitability,
            RasterToReport,
        ]


# =============================================================================
# TOOL 1 — Raster Crop Suitability
# =============================================================================
class RasterCropSuitability:
    def __init__(self):
        self.label = "1. ملاءمة المحاصيل — Raster"
        self.description = (
            "تحليل ملاءمة المحاصيل مباشرة من طبقات Raster (SoilGrids, WorldClim, SRTM)\n"
            "المخرج: خريطة Raster للفئات S1/S2/S3/N1/N2 + طبقة الدرجات"
        )
        self.canRunInBackground = False
        self.category = "1 - تحليل الملاءمة (Raster)"

    def getParameterInfo(self):
        params = []

        p = arcpy.Parameter("crop_type","المحصول","Input","GPString","Required")
        p.filter.type = "ValueList"
        p.filter.list = _crop_list()
        p.value = _crop_list()[0]
        params.append(p)

        raster_inputs = [
            ("ph_raster",    "طبقة pH التربة (SoilGrids: phh2o × 0.1)",        "Optional"),
            ("clay_raster",  "طبقة الطين Clay% (SoilGrids: clay_mean × 0.1)",   "Optional"),
            ("ec_raster",    "طبقة الملوحة EC dS/m (SoilGrids: ece_mean)",       "Optional"),
            ("depth_raster", "طبقة عمق التربة cm",                               "Optional"),
            ("slope_raster", "طبقة الانحدار % (مشتقة من SRTM)",                  "Optional"),
            ("temp_raster",  "طبقة متوسط الحرارة °C (WorldClim bio1 × 0.1)",     "Optional"),
            ("rain_raster",  "طبقة الأمطار السنوية مم (WorldClim bio12)",         "Optional"),
            ("drain_raster", "طبقة الصرف (رقم 1-6)",                              "Optional"),
        ]
        for name, label, req in raster_inputs:
            p = arcpy.Parameter(name, label, "Input", "GPRasterLayer", req)
            params.append(p)

        p = arcpy.Parameter("snap_raster","Snap Raster (مرجع الحجم والإسقاط)","Input","GPRasterLayer","Optional")
        params.append(p)

        p = arcpy.Parameter("out_score","طبقة الدرجات المخرجة (0-100)","Output","DERasterDataset","Required")
        params.append(p)
        p = arcpy.Parameter("out_class","طبقة فئات الملاءمة S1-N2","Output","DERasterDataset","Required")
        params.append(p)

        return params

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        return

    def updateMessages(self, parameters):
        return

    def execute(self, parameters, messages):
        if not _check_sa(messages):
            return

        crop_sel    = parameters[0].valueAsText
        ph_r        = parameters[1].valueAsText
        clay_r      = parameters[2].valueAsText
        ec_r        = parameters[3].valueAsText
        depth_r     = parameters[4].valueAsText
        slope_r     = parameters[5].valueAsText
        temp_r      = parameters[6].valueAsText
        rain_r      = parameters[7].valueAsText
        drain_r     = parameters[8].valueAsText
        snap_r      = parameters[9].valueAsText
        out_score   = parameters[10].valueAsText
        out_class   = parameters[11].valueAsText

        ckey  = _crop_key(crop_sel)
        crops = _load_crops()["crops"]
        cd    = crops[ckey]

        messages.addMessage(_header("ملاءمة المحاصيل — Raster", cd["name_ar"]))

        if snap_r:
            arcpy.env.snapRaster = snap_r

        scores = []
        weights = []

        # ── pH ──────────────────────────────────────────────────
        if ph_r:
            messages.addMessage("  ← معالجة طبقة pH ...")
            ph = Raster(ph_r)
            ph_score = _score_raster(ph, cd["ph_range"][0], cd["ph_range"][1], "range")
            scores.append(ph_score); weights.append(0.22)

        # ── Clay → texture proxy ────────────────────────────────
        if clay_r:
            messages.addMessage("  ← معالجة طبقة النسجة (Clay%) ...")
            clay = Raster(clay_r)
            # Optimal clay 20-40% for most crops
            clay_score = _score_raster(clay, 20, 40, "range")
            scores.append(clay_score); weights.append(0.18)

        # ── EC ──────────────────────────────────────────────────
        if ec_r:
            messages.addMessage("  ← معالجة طبقة الملوحة EC ...")
            ec = Raster(ec_r)
            ec_max = cd["ec_max"]
            ec_score = Con(ec <= ec_max, 100, Con(ec <= ec_max*2,
                           100 - ((ec - ec_max)/(ec_max)*100), 0))
            ec_score = Con(ec_score < 0, 0, ec_score)
            scores.append(ec_score); weights.append(0.16)

        # ── Depth ───────────────────────────────────────────────
        if depth_r:
            messages.addMessage("  ← معالجة طبقة عمق التربة ...")
            dep = Raster(depth_r)
            dmin = cd["soil_depth_min"]
            dep_score = Con(dep >= dmin, 100,
                            Con(dep >= dmin*0.5, (dep - dmin*0.5)/(dmin*0.5)*100, 0))
            dep_score = Con(dep_score > 100, 100, dep_score)
            scores.append(dep_score); weights.append(0.14)

        # ── Slope ───────────────────────────────────────────────
        if slope_r:
            messages.addMessage("  ← معالجة طبقة الانحدار ...")
            slp = Raster(slope_r)
            smax = cd["slope_max"]
            slp_score = Con(slp <= smax, 100,
                            Con(slp <= smax*2, 100 - ((slp-smax)/smax*100), 0))
            slp_score = Con(slp_score < 0, 0, slp_score)
            scores.append(slp_score); weights.append(0.12)

        # ── Temperature ─────────────────────────────────────────
        if temp_r:
            messages.addMessage("  ← معالجة طبقة الحرارة ...")
            tmp = Raster(temp_r)
            tmp_score = _score_raster(tmp, cd["temp_range"][0], cd["temp_range"][1], "range")
            scores.append(tmp_score); weights.append(0.10)

        # ── Rainfall ────────────────────────────────────────────
        if rain_r:
            messages.addMessage("  ← معالجة طبقة الأمطار ...")
            rn = Raster(rain_r)
            rn_score = _score_raster(rn, cd["rainfall_range"][0], cd["rainfall_range"][1], "range")
            scores.append(rn_score); weights.append(0.08)

        # ── Drainage ────────────────────────────────────────────
        if drain_r:
            messages.addMessage("  ← معالجة طبقة الصرف ...")
            dr = Raster(drain_r)
            dr_remap = RemapValue([[1,60],[2,100],[3,85],[4,50],[5,20],[6,5]])
            dr_score = Reclassify(dr, "Value", dr_remap)
            scores.append(dr_score); weights.append(0.08)

        if not scores:
            messages.addErrorMessage("لم يتم تحديد أي طبقة Raster. يرجى تحديد طبقة واحدة على الأقل.")
            return

        # ── Weighted average ─────────────────────────────────────
        messages.addMessage("\n  ← حساب المتوسط المرجح ...")
        total_w = sum(weights)
        combined = scores[0] * (weights[0]/total_w)
        for s, w in zip(scores[1:], weights[1:]):
            combined = combined + s * (w/total_w)

        combined.save(out_score)
        messages.addMessage("  ✓ طبقة الدرجات محفوظة: {}".format(out_score))

        # ── Classify ─────────────────────────────────────────────
        classified = _classify_raster(combined)
        classified.save(out_class)
        messages.addMessage("  ✓ طبقة الفئات محفوظة: {}".format(out_class))

        # ── Statistics ───────────────────────────────────────────
        messages.addMessage("\n  === إحصاء الفئات ===")
        try:
            result = arcpy.management.GetRasterProperties(combined, "MEAN")
            messages.addMessage("  متوسط الدرجة: {}".format(round(float(result.getOutput(0)),2)))
        except Exception:
            pass

        arcpy.CheckInExtension("Spatial")
        messages.addMessage("\n  ✅ اكتمل تحليل ملاءمة محصول {}".format(cd["name_ar"]))


# =============================================================================
# TOOL 2 — Raster Production Capacity
# =============================================================================
class RasterProductionCapacity:
    def __init__(self):
        self.label = "2. الإنتاجية المتوقعة — Raster"
        self.description = (
            "تقدير الغلة (طن/هكتار) مباشرة من خريطة الملاءمة الرقمية\n"
            "المخرج: خريطة Raster للإنتاجية المتوقعة + خريطة الدخل الصافي"
        )
        self.canRunInBackground = False
        self.category = "1 - تحليل الملاءمة (Raster)"

    def getParameterInfo(self):
        params = []
        p = arcpy.Parameter("suit_raster","طبقة درجة الملاءمة (0-100) من الأداة 1","Input","GPRasterLayer","Required")
        params.append(p)
        p = arcpy.Parameter("crop_type","المحصول","Input","GPString","Required")
        p.filter.type = "ValueList"; p.filter.list = _crop_list(); p.value = _crop_list()[0]
        params.append(p)
        p = arcpy.Parameter("crop_price","سعر المحصول (دولار/طن)","Input","GPDouble","Optional"); p.value = 300.0; params.append(p)
        p = arcpy.Parameter("prod_cost","تكلفة الإنتاج (دولار/هكتار)","Input","GPDouble","Optional"); p.value = 500.0; params.append(p)
        p = arcpy.Parameter("out_yield","خريطة الإنتاجية (طن/هكتار)","Output","DERasterDataset","Required"); params.append(p)
        p = arcpy.Parameter("out_income","خريطة الدخل الصافي (دولار/هكتار)","Output","DERasterDataset","Required"); params.append(p)
        p = arcpy.Parameter("out_class","خريطة فئة الإنتاجية","Output","DERasterDataset","Required"); params.append(p)
        return params

    def isLicensed(self): return True
    def updateParameters(self, p): return
    def updateMessages(self, p): return

    def execute(self, parameters, messages):
        if not _check_sa(messages): return
        suit_r      = parameters[0].valueAsText
        crop_sel    = parameters[1].valueAsText
        price       = parameters[2].value or 300.0
        cost        = parameters[3].value or 500.0
        out_yield   = parameters[4].valueAsText
        out_income  = parameters[5].valueAsText
        out_class   = parameters[6].valueAsText

        ckey = _crop_key(crop_sel)
        cd   = _load_crops()["crops"][ckey]
        max_y = cd["yield_potential_ton_ha"]

        messages.addMessage(_header("الإنتاجية المتوقعة", cd["name_ar"]))
        messages.addMessage("  الإنتاجية القصوى المحتملة: {} طن/هكتار".format(max_y))

        suit = Raster(suit_r)

        # Yield = max_yield × (score/100) — FAO parametric model
        yield_r = suit / 100.0 * max_y
        yield_r.save(out_yield)

        # Net income per ha
        income_r = yield_r * price - cost
        income_r.save(out_income)

        # Production class remap  (% of max yield)
        # 1=عالية جداً(>85%) 2=عالية(65-85%) 3=متوسطة(45-64%) 4=منخفضة(25-44%) 5=منخفضة جداً(<25%)
        pct = suit   # suit score ≈ pct of max
        class_r = Con(pct>=85,1,Con(pct>=65,2,Con(pct>=45,3,Con(pct>=25,4,5))))
        class_r.save(out_class)

        messages.addMessage("  ✓ خريطة الإنتاجية: {}".format(out_yield))
        messages.addMessage("  ✓ خريطة الدخل: {}".format(out_income))
        arcpy.CheckInExtension("Spatial")
        messages.addMessage("  ✅ اكتملت أداة الإنتاجية")


# =============================================================================
# TOOL 3 — Raster Soil Quality Index (SQI)
# =============================================================================
class RasterSoilQualityIndex:
    def __init__(self):
        self.label = "3. مؤشر جودة التربة SQI — Raster"
        self.description = (
            "حساب SQI مكانياً من طبقات Raster للتربة\n"
            "المعيار: USDA SQI المعدّل (Doran & Parkin 1994)\n"
            "المخرج: خريطة SQI (0-100) + خريطة التصنيف"
        )
        self.canRunInBackground = False
        self.category = "2 - جودة التربة (Raster)"

    def getParameterInfo(self):
        params = []
        layers = [
            ("ph_r",   "pH التربة (SoilGrids: phh2o × 0.1)","Optional"),
            ("om_r",   "المادة العضوية OM% (soc × 1.724)","Optional"),
            ("ec_r",   "الموصلية الكهربية EC (ece_mean)","Optional"),
            ("cec_r",  "السعة التبادلية CEC cmol/kg","Optional"),
            ("clay_r", "نسبة الطين Clay%","Optional"),
            ("bd_r",   "الكثافة الظاهرية BD g/cm³","Optional"),
            ("p_r",    "الفوسفور المتاح P ppm","Optional"),
            ("n_r",    "النيتروجين المتاح N ppm","Optional"),
        ]
        for n,l,r in layers:
            p = arcpy.Parameter(n, l, "Input","GPRasterLayer", r)
            params.append(p)
        p = arcpy.Parameter("out_sqi","طبقة SQI المخرجة (0-100)","Output","DERasterDataset","Required"); params.append(p)
        p = arcpy.Parameter("out_class","طبقة تصنيف جودة التربة","Output","DERasterDataset","Required"); params.append(p)
        return params

    def isLicensed(self): return True
    def updateParameters(self, p): return
    def updateMessages(self, p): return

    def execute(self, parameters, messages):
        if not _check_sa(messages): return
        ph_r,om_r,ec_r,cec_r,clay_r,bd_r,p_r,n_r = [parameters[i].valueAsText for i in range(8)]
        out_sqi   = parameters[8].valueAsText
        out_class = parameters[9].valueAsText

        messages.addMessage(_header("مؤشر جودة التربة SQI"))

        chem_scores = []; chem_w = []
        phys_scores = []; phys_w = []
        nutr_scores = []; nutr_w = []

        # Chemical quality
        if ph_r:
            ph = Raster(ph_r)
            ph_s = _score_raster(ph, 6.0, 7.5, "range")
            chem_scores.append(ph_s); chem_w.append(0.35)

        if ec_r:
            ec = Raster(ec_r)
            ec_s = Con(ec<=2,100,Con(ec<=4,80,Con(ec<=8,60,Con(ec<=16,30,5))))
            chem_scores.append(ec_s); chem_w.append(0.30)

        if om_r:
            om = Raster(om_r)
            om_s = Con(om>=3,100,Con(om>=2,80,Con(om>=1,60,Con(om>=0.5,40,20))))
            chem_scores.append(om_s); chem_w.append(0.35)
            nutr_scores.append(om_s); nutr_w.append(0.40)

        if cec_r:
            cec = Raster(cec_r)
            cec_s = Con(cec>=30,100,Con(cec>=20,85,Con(cec>=10,65,Con(cec>=5,40,20))))
            chem_scores.append(cec_s); chem_w.append(0.15) if not om_r else None

        # Physical quality
        if clay_r:
            clay = Raster(clay_r)
            clay_s = _score_raster(clay, 20, 40, "range")
            phys_scores.append(clay_s); phys_w.append(0.50)

        if bd_r:
            bd = Raster(bd_r)
            bd_s = Con(bd<=1.4,Con(bd>=1.1,100,70),Con(bd<=1.6,50,20))
            phys_scores.append(bd_s); phys_w.append(0.50)

        # Nutrient quality
        if p_r:
            phos = Raster(p_r)
            p_s = Con(phos>=25,100,Con(phos>=15,80,Con(phos>=8,60,Con(phos>=4,40,20))))
            nutr_scores.append(p_s); nutr_w.append(0.30)

        if n_r:
            nit = Raster(n_r)
            n_s = Con(nit>=100,100,Con(nit>=60,80,Con(nit>=30,60,Con(nit>=15,40,20))))
            nutr_scores.append(n_s); nutr_w.append(0.30)

        def weighted_raster(sc, wt):
            if not sc: return None
            total = sum(wt)
            result = sc[0] * (wt[0]/total)
            for s,w in zip(sc[1:],wt[1:]):
                result = result + s*(w/total)
            return result

        chem = weighted_raster(chem_scores, chem_w)
        phys = weighted_raster(phys_scores, phys_w)
        nutr = weighted_raster(nutr_scores, nutr_w)

        components = [c for c in [chem,phys,nutr] if c is not None]
        comp_w     = [0.40,0.35,0.25][:len(components)]

        if not components:
            messages.addErrorMessage("يرجى تحديد طبقة Raster واحدة على الأقل")
            return

        sqi = weighted_raster(components, comp_w)
        sqi.save(out_sqi)

        # Class: 1=ممتازة(≥80) 2=جيدة(65-79) 3=متوسطة(50-64) 4=ضعيفة(35-49) 5=متدهورة(<35)
        cls = Con(sqi>=80,1,Con(sqi>=65,2,Con(sqi>=50,3,Con(sqi>=35,4,5))))
        cls.save(out_class)

        messages.addMessage("  ✓ طبقة SQI: {}".format(out_sqi))
        messages.addMessage("  ✓ طبقة التصنيف: {}".format(out_class))
        arcpy.CheckInExtension("Spatial")
        messages.addMessage("  ✅ اكتمل حساب SQI")


# =============================================================================
# TOOL 4 — Raster Irrigation Suitability
# =============================================================================
class RasterIrrigationSuitability:
    def __init__(self):
        self.label = "4. ملاءمة الري — Raster"
        self.description = (
            "تقييم ملاءمة 3 أنظمة ري (سطحي، رش، تنقيط) مكانياً\n"
            "المخرج: 3 خرائط Raster لكل نظام + خريطة أفضل نظام"
        )
        self.canRunInBackground = False
        self.category = "3 - المياه والري (Raster)"

    def getParameterInfo(self):
        params = []
        layers = [
            ("slope_r", "طبقة الانحدار % (SRTM)","Optional"),
            ("depth_r", "طبقة عمق التربة cm","Optional"),
            ("ec_r",    "طبقة الملوحة EC","Optional"),
            ("perc_r",  "طبقة النفاذية cm/hr","Optional"),
            ("drain_r", "طبقة الصرف (رقم 1-6)","Optional"),
        ]
        for n,l,r in layers:
            p = arcpy.Parameter(n,l,"Input","GPRasterLayer",r); params.append(p)
        p = arcpy.Parameter("out_surface","ملاءمة الري السطحي","Output","DERasterDataset","Required"); params.append(p)
        p = arcpy.Parameter("out_sprinkler","ملاءمة الري بالرش","Output","DERasterDataset","Required"); params.append(p)
        p = arcpy.Parameter("out_drip","ملاءمة الري بالتنقيط","Output","DERasterDataset","Required"); params.append(p)
        p = arcpy.Parameter("out_best","خريطة أفضل نظام ري (1=سطحي، 2=رش، 3=تنقيط)","Output","DERasterDataset","Required"); params.append(p)
        return params

    def isLicensed(self): return True
    def updateParameters(self,p): return
    def updateMessages(self,p): return

    def execute(self, parameters, messages):
        if not _check_sa(messages): return
        slope_r,depth_r,ec_r,perc_r,drain_r = [parameters[i].valueAsText for i in range(5)]
        out_surf,out_spr,out_drip,out_best   = [parameters[i].valueAsText for i in range(5,9)]

        messages.addMessage(_header("ملاءمة الري — Raster"))

        def get(r): return Raster(r) if r else None
        slp  = get(slope_r)
        dep  = get(depth_r)
        ec   = get(ec_r)
        perc = get(perc_r)
        drn  = get(drain_r)

        # ── Surface irrigation ───────────────────────────────────
        s_scores=[]; s_w=[]
        if slp:  s_scores.append(Con(slp<=2,100,Con(slp<=5,80,Con(slp<=8,60,Con(slp<=12,40,15))))); s_w.append(0.40)
        if dep:  s_scores.append(Con(dep>=80,100,Con(dep>=60,80,Con(dep>=40,60,30)))); s_w.append(0.30)
        if ec:   s_scores.append(Con(ec<=2,100,Con(ec<=4,80,Con(ec<=8,60,Con(ec<=16,30,5))))); s_w.append(0.30)

        # ── Sprinkler ────────────────────────────────────────────
        r_scores=[]; r_w=[]
        if slp:  r_scores.append(Con(slp<=5,100,Con(slp<=10,85,Con(slp<=15,70,Con(slp<=20,50,20))))); r_w.append(0.30)
        if dep:  r_scores.append(Con(dep>=50,100,Con(dep>=30,75,40))); r_w.append(0.25)
        if ec:   r_scores.append(Con(ec<=4,100,Con(ec<=8,75,Con(ec<=12,50,20)))); r_w.append(0.25)
        if perc: r_scores.append(Con(perc>=2,Con(perc<=20,100,70),40)); r_w.append(0.20)

        # ── Drip ─────────────────────────────────────────────────
        d_scores=[]; d_w=[]
        if slp:  d_scores.append(Con(slp<=10,100,Con(slp<=20,85,Con(slp<=30,65,40)))); d_w.append(0.20)
        if dep:  d_scores.append(Con(dep>=30,100,Con(dep>=20,80,50))); d_w.append(0.20)
        if ec:   d_scores.append(Con(ec<=6,100,Con(ec<=12,80,Con(ec<=20,55,25)))); d_w.append(0.60)

        def wt_avg(sc,wt):
            if not sc: return None
            t=sum(wt); r=sc[0]*(wt[0]/t)
            for s,w in zip(sc[1:],wt[1:]): r=r+s*(w/t)
            return r

        surf_r = wt_avg(s_scores,s_w)
        spr_r  = wt_avg(r_scores,r_w)
        drip_r = wt_avg(d_scores,d_w)

        if surf_r: surf_r.save(out_surf); messages.addMessage("  ✓ ملاءمة الري السطحي: {}".format(out_surf))
        if spr_r:  spr_r.save(out_spr);  messages.addMessage("  ✓ ملاءمة الري بالرش: {}".format(out_spr))
        if drip_r: drip_r.save(out_drip);messages.addMessage("  ✓ ملاءمة التنقيط: {}".format(out_drip))

        # Best system (highest score pixel by pixel)
        if surf_r and spr_r and drip_r:
            best = Con(surf_r>=spr_r,Con(surf_r>=drip_r,1,3),Con(spr_r>=drip_r,2,3))
            best.save(out_best)
            messages.addMessage("  ✓ خريطة أفضل نظام: {}".format(out_best))

        arcpy.CheckInExtension("Spatial")
        messages.addMessage("  ✅ اكتمل تقييم ملاءمة الري")


# =============================================================================
# TOOL 5 — Raster Land Degradation Risk
# =============================================================================
class RasterLandDegradation:
    def __init__(self):
        self.label = "5. خطر تدهور الأراضي — Raster"
        self.description = (
            "تقييم مخاطر التملح، التصحر، التغدق مكانياً\n"
            "المخرج: خريطة Raster للتدهور الإجمالي وخرائط كل نوع"
        )
        self.canRunInBackground = False
        self.category = "4 - تدهور الأراضي (Raster)"

    def getParameterInfo(self):
        params = []
        layers = [
            ("ec_r",     "طبقة الملوحة EC (SoilGrids)","Optional"),
            ("sar_r",    "طبقة SAR/ESP (نسبة الصوديوم)","Optional"),
            ("slope_r",  "طبقة الانحدار % (SRTM)","Optional"),
            ("veg_r",    "طبقة الغطاء النباتي % (NDVI×100 أو WorldCover)","Optional"),
            ("wt_r",     "طبقة عمق المياه الجوفية م","Optional"),
        ]
        for n,l,r in layers:
            p = arcpy.Parameter(n,l,"Input","GPRasterLayer",r); params.append(p)
        for n,l in [("out_salin","خريطة خطر التملح"),("out_alkali","خريطة خطر القلوية"),
                    ("out_erosion","خريطة خطر الانجراف"),("out_wlog","خريطة خطر التغدق"),
                    ("out_overall","خريطة التدهور الإجمالي (0-100)")]:
            p = arcpy.Parameter(n,l,"Output","DERasterDataset","Required"); params.append(p)
        return params

    def isLicensed(self): return True
    def updateParameters(self,p): return
    def updateMessages(self,p): return

    def execute(self, parameters, messages):
        if not _check_sa(messages): return
        ec_r,sar_r,slope_r,veg_r,wt_r = [parameters[i].valueAsText for i in range(5)]
        out_sal,out_alk,out_er,out_wl,out_ov = [parameters[i].valueAsText for i in range(5,10)]

        messages.addMessage(_header("خطر تدهور الأراضي"))

        comp=[]; comp_w=[]

        if ec_r:
            ec = Raster(ec_r)
            sal = Con(ec<=2,5,Con(ec<=4,20,Con(ec<=8,50,Con(ec<=16,80,100))))
            sal.save(out_sal)
            comp.append(sal); comp_w.append(0.25)
            messages.addMessage("  ✓ خريطة التملح محفوظة")

        if sar_r:
            sar = Raster(sar_r)
            alk = Con(sar<=10,15,Con(sar<=18,40,Con(sar<=26,70,95)))
            alk.save(out_alk)
            comp.append(alk); comp_w.append(0.15)
            messages.addMessage("  ✓ خريطة القلوية محفوظة")

        if slope_r and veg_r:
            slp = Raster(slope_r); veg = Raster(veg_r)
            er = (100-veg)*0.6 + Con(slp<=5,0,Con(slp<=10,20,Con(slp<=20,50,80)))*0.4
            er = Con(er>100,100,er)
            er.save(out_er)
            comp.append(er); comp_w.append(0.30)
            messages.addMessage("  ✓ خريطة الانجراف محفوظة")
        elif slope_r:
            slp = Raster(slope_r)
            er = Con(slp<=5,10,Con(slp<=10,30,Con(slp<=20,65,90)))
            er.save(out_er)
            comp.append(er); comp_w.append(0.25)
            messages.addMessage("  ✓ خريطة الانجراف (بدون غطاء نباتي) محفوظة")

        if wt_r:
            wt = Raster(wt_r)
            wl = Con(wt>=3,5,Con(wt>=1.5,30,Con(wt>=0.8,65,95)))
            wl.save(out_wlog)
            comp.append(wl); comp_w.append(0.20)
            messages.addMessage("  ✓ خريطة التغدق محفوظة")

        if comp:
            t=sum(comp_w); ov=comp[0]*(comp_w[0]/t)
            for c,w in zip(comp[1:],comp_w[1:]): ov=ov+c*(w/t)
            ov = Con(ov>100,100,Con(ov<0,0,ov))
            ov.save(out_ov)
            messages.addMessage("  ✓ خريطة التدهور الإجمالي: {}".format(out_ov))

        arcpy.CheckInExtension("Spatial")
        messages.addMessage("  ✅ اكتمل تقييم التدهور")


# =============================================================================
# TOOL 6 — Raster Erosion (RUSLE)
# =============================================================================
class RasterErosionRUSLE:
    def __init__(self):
        self.label = "6. التعرية المائية RUSLE — Raster"
        self.description = (
            "حساب A = R × K × LS × C × P على مستوى البكسل\n"
            "المعادلة: Wischmeier & Smith (1978) المعدّلة — RUSLE\n"
            "المخرج: خريطة فقدان التربة (طن/هكتار/سنة)"
        )
        self.canRunInBackground = False
        self.category = "4 - تدهور الأراضي (Raster)"

    def getParameterInfo(self):
        params = []
        layers = [
            ("dem_r",  "نموذج الارتفاع الرقمي DEM (SRTM/Copernicus)","Required"),
            ("k_r",    "عامل قابلية التربة K (Raster أو قيمة ثابتة)","Optional"),
            ("c_r",    "عامل الغطاء النباتي C (من NDVI)","Optional"),
            ("p_r",    "عامل الممارسات الحفظية P","Optional"),
            ("r_r",    "عامل التعرية المطرية R (خريطة أو ثابت)","Optional"),
        ]
        for n,l,r in layers:
            p = arcpy.Parameter(n,l,"Input","GPRasterLayer",r); params.append(p)
        p = arcpy.Parameter("r_default","قيمة R الافتراضية (إذا لم تُحدد طبقة)","Input","GPDouble","Optional"); p.value=150.0; params.append(p)
        p = arcpy.Parameter("k_default","قيمة K الافتراضية","Input","GPDouble","Optional"); p.value=0.30; params.append(p)
        p = arcpy.Parameter("c_default","قيمة C الافتراضية","Input","GPDouble","Optional"); p.value=0.50; params.append(p)
        p = arcpy.Parameter("p_default","قيمة P الافتراضية","Input","GPDouble","Optional"); p.value=1.00; params.append(p)
        p = arcpy.Parameter("out_soil_loss","خريطة فقدان التربة A (طن/هكتار/سنة)","Output","DERasterDataset","Required"); params.append(p)
        p = arcpy.Parameter("out_class","خريطة فئة التعرية","Output","DERasterDataset","Required"); params.append(p)
        return params

    def isLicensed(self): return True
    def updateParameters(self,p): return
    def updateMessages(self,p): return

    def execute(self, parameters, messages):
        if not _check_sa(messages): return
        dem_r  = parameters[0].valueAsText
        k_r    = parameters[1].valueAsText
        c_r    = parameters[2].valueAsText
        p_r_l  = parameters[3].valueAsText
        r_r    = parameters[4].valueAsText
        r_def  = parameters[5].value or 150.0
        k_def  = parameters[6].value or 0.30
        c_def  = parameters[7].value or 0.50
        p_def  = parameters[8].value or 1.00
        out_a  = parameters[9].valueAsText
        out_cl = parameters[10].valueAsText

        messages.addMessage(_header("التعرية المائية RUSLE"))

        dem = Raster(dem_r)
        arcpy.env.snapRaster = dem_r

        # ── Slope in degrees & percent ───────────────────────────
        messages.addMessage("  ← استخراج الانحدار من DEM ...")
        slope_deg = Slope(dem, "DEGREE")
        slope_pct = Slope(dem, "PERCENT_RISE")

        # ── LS factor — McCool et al. (1989) ────────────────────
        messages.addMessage("  ← حساب عامل LS ...")
        # Flow accumulation for slope length
        fill_dem  = Fill(dem)
        flow_dir  = FlowDirection(fill_dem)
        flow_acc  = FlowAccumulation(flow_dir)
        cell_size = float(arcpy.management.GetRasterProperties(dem,"CELLSIZEX").getOutput(0))
        slope_len = (flow_acc + 1) * cell_size  # meters

        slope_r_rad = slope_deg * math.pi / 180.0
        m_exp = Con(slope_pct < 1, 0.2,
                    Con(slope_pct < 3, 0.3,
                    Con(slope_pct < 5, 0.4, 0.5)))

        L_factor = Power(slope_len / 22.13, m_exp)
        S_factor = Con(slope_deg < 5,
                       10.8 * Sin(slope_r_rad) + 0.03,
                       16.8 * Sin(slope_r_rad) - 0.50)
        LS = L_factor * S_factor
        messages.addMessage("  ✓ عامل LS محسوب")

        # ── R factor ─────────────────────────────────────────────
        R = Raster(r_r) if r_r else r_def
        messages.addMessage("  ✓ عامل R: {}".format("خريطة" if r_r else r_def))

        # ── K factor ─────────────────────────────────────────────
        K = Raster(k_r) if k_r else k_def
        messages.addMessage("  ✓ عامل K: {}".format("خريطة" if k_r else k_def))

        # ── C factor (from NDVI) ─────────────────────────────────
        if c_r:
            ndvi = Raster(c_r)
            # C = exp(−2.5 × NDVI / (1.2 − NDVI))  — van der Knijff et al.
            C = Exp(-2.5 * ndvi / (1.2 - ndvi))
            C = Con(C>1,1,Con(C<0,0.01,C))
            messages.addMessage("  ✓ عامل C محسوب من NDVI")
        else:
            C = c_def
            messages.addMessage("  ✓ عامل C الافتراضي: {}".format(c_def))

        # ── P factor ─────────────────────────────────────────────
        P = Raster(p_r_l) if p_r_l else p_def

        # ── RUSLE: A = R × K × LS × C × P ──────────────────────
        messages.addMessage("  ← حساب RUSLE: A = R × K × LS × C × P ...")
        A = R * K * LS * C * P
        A = Con(A < 0, 0, A)
        A.save(out_a)
        messages.addMessage("  ✓ خريطة فقدان التربة: {}".format(out_a))

        # ── Erosion class ────────────────────────────────────────
        # 1=منخفض(<5), 2=خفيف(5-15), 3=متوسط(15-30), 4=عالٍ(30-50), 5=شديد(50-100), 6=حرج(>100)
        cls = Con(A<5,1,Con(A<15,2,Con(A<30,3,Con(A<50,4,Con(A<100,5,6)))))
        cls.save(out_cl)
        messages.addMessage("  ✓ خريطة فئة التعرية: {}".format(out_cl))

        arcpy.CheckInExtension("Spatial")
        messages.addMessage("  ✅ اكتمل حساب RUSLE")


# =============================================================================
# TOOL 7 — Raster SOC Stock
# =============================================================================
class RasterSOCStock:
    def __init__(self):
        self.label = "7. مخزون الكربون العضوي SOC — Raster"
        self.description = (
            "حساب مخزون SOC (طن C/هكتار) ومعادل CO₂ مكانياً\n"
            "المعادلة: IPCC Tier 1 — SOC = OC% × BD × Depth × 100"
        )
        self.canRunInBackground = False
        self.category = "2 - جودة التربة (Raster)"

    def getParameterInfo(self):
        params = []
        p = arcpy.Parameter("oc_r","طبقة الكربون العضوي OC% (SoilGrids: soc_mean × 0.1)","Input","GPRasterLayer","Required"); params.append(p)
        p = arcpy.Parameter("bd_r","طبقة الكثافة الظاهرية BD g/cm³ (SoilGrids: bdod × 0.01)","Input","GPRasterLayer","Required"); params.append(p)
        p = arcpy.Parameter("depth","عمق التحليل cm (افتراضي 30)","Input","GPDouble","Optional"); p.value=30.0; params.append(p)
        p = arcpy.Parameter("carbon_price","سعر اعتمادات الكربون (دولار/طن CO₂)","Input","GPDouble","Optional"); p.value=15.0; params.append(p)
        p = arcpy.Parameter("out_soc","خريطة SOC (طن C/هكتار)","Output","DERasterDataset","Required"); params.append(p)
        p = arcpy.Parameter("out_co2","خريطة معادل CO₂ (طن/هكتار)","Output","DERasterDataset","Required"); params.append(p)
        p = arcpy.Parameter("out_value","خريطة قيمة الكربون (دولار/هكتار)","Output","DERasterDataset","Required"); params.append(p)
        return params

    def isLicensed(self): return True
    def updateParameters(self,p): return
    def updateMessages(self,p): return

    def execute(self, parameters, messages):
        if not _check_sa(messages): return
        oc_r   = parameters[0].valueAsText
        bd_r   = parameters[1].valueAsText
        depth  = parameters[2].value or 30.0
        cprice = parameters[3].value or 15.0
        out_s  = parameters[4].valueAsText
        out_c  = parameters[5].valueAsText
        out_v  = parameters[6].valueAsText

        messages.addMessage(_header("مخزون الكربون العضوي SOC"))
        messages.addMessage("  عمق التحليل: {} سم".format(depth))

        oc = Raster(oc_r)
        bd = Raster(bd_r)

        # SOC (Mg C/ha) = OC% / 100 × BD (g/cm³) × Depth (cm) × 10000 cm²/m² / 100
        #               = OC% × BD × Depth (simplified)
        soc = (oc / 100.0) * bd * depth * 100.0  # ton C/ha
        soc = Con(soc < 0, 0, soc)
        soc.save(out_s)
        messages.addMessage("  ✓ خريطة SOC: {}".format(out_s))

        # CO2 equivalent: CO2 = C × (44/12) = C × 3.667
        co2 = soc * 3.667
        co2.save(out_c)
        messages.addMessage("  ✓ خريطة CO₂: {}".format(out_c))

        # Carbon credit value
        val = co2 * cprice
        val.save(out_v)
        messages.addMessage("  ✓ خريطة قيمة الكربون: {}".format(out_v))

        arcpy.CheckInExtension("Spatial")
        messages.addMessage("  ✅ اكتمل حساب SOC")


# =============================================================================
# TOOL 8 — Raster Multi-Criteria (MCLE)
# =============================================================================
class RasterMultiCriteria:
    def __init__(self):
        self.label = "8. التقييم متعدد المعايير MCLE — Raster"
        self.description = (
            "دمج خرائط الملاءمة وجودة التربة والتدهور والري في تقييم شامل\n"
            "المنهجية: Analytic Hierarchy Process (AHP) — Saaty (1980)\n"
            "المخرج: خريطة MCLE الشاملة + فئة الأولوية"
        )
        self.canRunInBackground = False
        self.category = "5 - التقييم الشامل (Raster)"

    def getParameterInfo(self):
        params = []
        layers_w = [
            ("suit_r",  "خريطة الملاءمة (SUIT_SCORE 0-100)",     "Optional", 35),
            ("sqi_r",   "خريطة جودة التربة (SQI 0-100)",         "Optional", 25),
            ("deg_r",   "خريطة التدهور (0-100، عكسي)",           "Optional", 20),
            ("irr_r",   "خريطة ملاءمة الري (0-100)",             "Optional", 15),
            ("erosion_r","خريطة التعرية (0-100، عكسي)",          "Optional",  5),
        ]
        for n,l,r,_ in layers_w:
            p = arcpy.Parameter(n,l,"Input","GPRasterLayer",r); params.append(p)
        for _,_,_,wdef in layers_w:
            p = arcpy.Parameter("w_{}".format(_),"{} وزن (%)".format(_),"Input","GPLong","Optional")
            p.value=wdef; params.append(p)
        p = arcpy.Parameter("out_mcle","خريطة MCLE الشاملة (0-100)","Output","DERasterDataset","Required"); params.append(p)
        p = arcpy.Parameter("out_priority","خريطة أولوية الاستخدام (1-5)","Output","DERasterDataset","Required"); params.append(p)
        return params

    def isLicensed(self): return True
    def updateParameters(self,p): return
    def updateMessages(self,p): return

    def execute(self, parameters, messages):
        if not _check_sa(messages): return
        suit_r,sqi_r,deg_r,irr_r,er_r = [parameters[i].valueAsText for i in range(5)]
        w_suit,w_sqi,w_deg,w_irr,w_er = [parameters[i].value or v for i,v in zip(range(5,10),[35,25,20,15,5])]
        out_mc = parameters[10].valueAsText
        out_pr = parameters[11].valueAsText

        messages.addMessage(_header("التقييم متعدد المعايير MCLE"))

        comp=[]; comp_w=[]
        if suit_r:  comp.append(Raster(suit_r)); comp_w.append(w_suit)
        if sqi_r:   comp.append(Raster(sqi_r));  comp_w.append(w_sqi)
        if deg_r:   comp.append(100-Raster(deg_r)); comp_w.append(w_deg)  # inverted
        if irr_r:   comp.append(Raster(irr_r));  comp_w.append(w_irr)
        if er_r:    comp.append(100-Raster(er_r)); comp_w.append(w_er)   # inverted

        if not comp:
            messages.addErrorMessage("يجب تحديد طبقة Raster واحدة على الأقل")
            return

        t=sum(comp_w); mc=comp[0]*(comp_w[0]/t)
        for c,w in zip(comp[1:],comp_w[1:]): mc=mc+c*(w/t)
        mc = Con(mc>100,100,Con(mc<0,0,mc))
        mc.save(out_mc)
        messages.addMessage("  ✓ خريطة MCLE: {}".format(out_mc))

        # Priority 1(best)=≥80 … 5(worst)=<35
        pr = Con(mc>=80,1,Con(mc>=65,2,Con(mc>=50,3,Con(mc>=35,4,5))))
        pr.save(out_pr)
        messages.addMessage("  ✓ خريطة الأولوية: {}".format(out_pr))

        arcpy.CheckInExtension("Spatial")
        messages.addMessage("  ✅ اكتمل MCLE")


# =============================================================================
# TOOL 9 — Raster Economic Evaluation
# =============================================================================
class RasterEconomicEval:
    def __init__(self):
        self.label = "9. التقييم الاقتصادي — Raster"
        self.description = (
            "حساب NPV والعائد الاقتصادي مكانياً من خريطة الإنتاجية\n"
            "المخرج: خرائط Raster للإيراد والدخل الصافي والقيمة الحالية"
        )
        self.canRunInBackground = False
        self.category = "6 - الاقتصاد (Raster)"

    def getParameterInfo(self):
        params = []
        p = arcpy.Parameter("yield_r","خريطة الإنتاجية (طن/هكتار) من الأداة 2","Input","GPRasterLayer","Required"); params.append(p)
        for n,l,v in [("price","سعر المحصول (دولار/طن)",300),
                      ("input_cost","تكلفة المدخلات (دولار/هكتار)",400),
                      ("labor_cost","تكلفة العمالة (دولار/هكتار)",150),
                      ("water_cost","تكلفة المياه (دولار/هكتار)",100),
                      ("discount","معدل الخصم %",10),
                      ("years","سنوات المشروع",20)]:
            p = arcpy.Parameter(n,l,"Input","GPDouble","Optional"); p.value=v; params.append(p)
        p = arcpy.Parameter("out_revenue","خريطة الإيراد (دولار/هكتار)","Output","DERasterDataset","Required"); params.append(p)
        p = arcpy.Parameter("out_net","خريطة الدخل الصافي (دولار/هكتار)","Output","DERasterDataset","Required"); params.append(p)
        p = arcpy.Parameter("out_npv","خريطة NPV (دولار/هكتار)","Output","DERasterDataset","Required"); params.append(p)
        p = arcpy.Parameter("out_bcr","خريطة نسبة العائد BCR","Output","DERasterDataset","Required"); params.append(p)
        return params

    def isLicensed(self): return True
    def updateParameters(self,p): return
    def updateMessages(self,p): return

    def execute(self, parameters, messages):
        if not _check_sa(messages): return
        yield_r = parameters[0].valueAsText
        price   = parameters[1].value or 300
        ic      = parameters[2].value or 400
        lc      = parameters[3].value or 150
        wc      = parameters[4].value or 100
        disc    = (parameters[5].value or 10) / 100.0
        yrs     = int(parameters[6].value or 20)
        out_rev = parameters[7].valueAsText
        out_net = parameters[8].valueAsText
        out_npv = parameters[9].valueAsText
        out_bcr = parameters[10].valueAsText

        messages.addMessage(_header("التقييم الاقتصادي"))

        yd = Raster(yield_r)
        total_cost = ic + lc + wc

        rev = yd * price
        net = rev - total_cost
        rev.save(out_rev)
        net.save(out_net)
        messages.addMessage("  ✓ خرائط الإيراد والدخل محفوظة")

        # NPV = net × Σ(1/(1+r)^t) — annuity factor
        annuity = sum(1.0/((1+disc)**t) for t in range(1, yrs+1))
        npv = net * annuity
        npv.save(out_npv)

        pv_cost = total_cost * annuity
        pv_ben  = rev * annuity
        bcr     = pv_ben / pv_cost
        bcr = Con(bcr>10,10,Con(bcr<0,0,bcr))
        bcr.save(out_bcr)

        messages.addMessage("  معامل الاستحقاق (Annuity Factor): {}".format(round(annuity,3)))
        messages.addMessage("  ✓ خرائط NPV وBCR محفوظة")
        arcpy.CheckInExtension("Spatial")
        messages.addMessage("  ✅ اكتمل التقييم الاقتصادي")


# =============================================================================
# TOOL 10 — Raster Land Use Optimization
# =============================================================================
class RasterLandUseOptimize:
    def __init__(self):
        self.label = "10. تحسين استخدام الأراضي — Raster"
        self.description = (
            "اقتراح التوزيع الأمثل لاستخدامات الأراضي بناءً على MCLE والانحدار والتدهور\n"
            "المخرج: خريطة Raster للمناطق الزراعية والحماية والغابات"
        )
        self.canRunInBackground = False
        self.category = "6 - الاقتصاد (Raster)"

    def getParameterInfo(self):
        params = []
        p = arcpy.Parameter("mcle_r","خريطة MCLE (0-100) من الأداة 8","Input","GPRasterLayer","Required"); params.append(p)
        p = arcpy.Parameter("slope_r","طبقة الانحدار %","Input","GPRasterLayer","Optional"); params.append(p)
        p = arcpy.Parameter("deg_r","خريطة التدهور (0-100)","Input","GPRasterLayer","Optional"); params.append(p)
        p = arcpy.Parameter("goal","هدف التخطيط","Input","GPString","Required")
        p.filter.type="ValueList"
        p.filter.list=["تعظيم الإنتاج الزراعي","التوازن بين الإنتاج والبيئة","الاستدامة البيئية","الأمن الغذائي والتنوع"]
        p.value="التوازن بين الإنتاج والبيئة"; params.append(p)
        p = arcpy.Parameter("out_zones","خريطة مناطق الاستخدام","Output","DERasterDataset","Required")
        # 1=زراعة مكثفة 2=زراعة متوسطة 3=مراعي وأشجار 4=غابات وحماية 5=إعادة تأهيل
        params.append(p)
        return params

    def isLicensed(self): return True
    def updateParameters(self,p): return
    def updateMessages(self,p): return

    def execute(self, parameters, messages):
        if not _check_sa(messages): return
        mcle_r = parameters[0].valueAsText
        slope_r= parameters[1].valueAsText
        deg_r  = parameters[2].valueAsText
        goal   = parameters[3].valueAsText
        out_z  = parameters[4].valueAsText

        messages.addMessage(_header("تحسين استخدام الأراضي"))
        messages.addMessage("  الهدف: {}".format(goal))

        mc = Raster(mcle_r)

        # Thresholds based on planning goal
        thresholds = {
            "تعظيم الإنتاج الزراعي":         (40, 25, 15),
            "التوازن بين الإنتاج والبيئة":    (55, 40, 25),
            "الاستدامة البيئية":              (65, 50, 35),
            "الأمن الغذائي والتنوع":          (50, 35, 20),
        }
        t1, t2, t3 = thresholds.get(goal, (55, 40, 25))

        if slope_r and deg_r:
            slp = Raster(slope_r); deg = Raster(deg_r)
            zones = Con((mc>=t1)&(slp<15)&(deg<50), 1,
                    Con((mc>=t2)&(slp<25), 2,
                    Con((mc>=t3)|(slp<30), 3,
                    Con(deg>75, 5, 4))))
        elif slope_r:
            slp = Raster(slope_r)
            zones = Con((mc>=t1)&(slp<15),1,Con((mc>=t2)&(slp<25),2,Con(slp<30,3,4)))
        else:
            zones = Con(mc>=t1,1,Con(mc>=t2,2,Con(mc>=t3,3,Con(mc>=25,4,5))))

        zones.save(out_z)
        messages.addMessage("  ✓ خريطة المناطق (1=زراعة مكثفة، 5=إعادة تأهيل)")
        messages.addMessage("  ✓ المخرج: {}".format(out_z))
        arcpy.CheckInExtension("Spatial")
        messages.addMessage("  ✅ اكتمل تحسين الاستخدام")


# =============================================================================
# TOOL 11 — Environmental Impact
# =============================================================================
class RasterEnvironmentalImpact:
    def __init__(self):
        self.label = "11. الأثر البيئي EIA — Raster"
        self.description = (
            "تقييم الأثر البيئي للزراعة مكانياً بناءً على خصائص الموقع\n"
            "المخرج: خريطة درجة الأثر البيئي + توصية EIA"
        )
        self.canRunInBackground = False
        self.category = "7 - البيئة (Raster)"

    def getParameterInfo(self):
        params = []
        p = arcpy.Parameter("activity","نوع النشاط الزراعي","Input","GPString","Required")
        p.filter.type="ValueList"
        p.filter.list=["زراعة مروية مكثفة","زراعة بعلية","مراعي","تشجير","زراعة محمية","زراعة عضوية"]
        p.value="زراعة مروية مكثفة"; params.append(p)
        for n,l in [("slope_r","طبقة الانحدار"),("veg_r","طبقة الغطاء النباتي %"),
                    ("ec_r","طبقة الملوحة"),("water_dist_r","المسافة من مجاري المياه م")]:
            p = arcpy.Parameter(n,l,"Input","GPRasterLayer","Optional"); params.append(p)
        p = arcpy.Parameter("out_eia","خريطة درجة الأثر البيئي (0-100)","Output","DERasterDataset","Required"); params.append(p)
        p = arcpy.Parameter("out_decision","خريطة القرار البيئي (1=موافقة، 4=رفض)","Output","DERasterDataset","Required"); params.append(p)
        return params

    def isLicensed(self): return True
    def updateParameters(self,p): return
    def updateMessages(self,p): return

    def execute(self, parameters, messages):
        if not _check_sa(messages): return
        activity = parameters[0].valueAsText
        slope_r,veg_r,ec_r,wdist_r = [parameters[i].valueAsText for i in range(1,5)]
        out_eia = parameters[5].valueAsText
        out_dec = parameters[6].valueAsText

        messages.addMessage(_header("التقييم البيئي EIA"))

        base = {"زراعة مروية مكثفة":70,"زراعة بعلية":25,"مراعي":15,
                "تشجير":5,"زراعة محمية":40,"زراعة عضوية":15}
        base_impact = base.get(activity, 40)

        impact = base_impact  # scalar start

        comp=[base_impact]; cw=[0.40]

        if slope_r:
            slp=Raster(slope_r)
            slope_impact = Con(slp>20,80,Con(slp>10,50,Con(slp>5,30,10)))
            comp.append(slope_impact); cw.append(0.25)

        if ec_r:
            ec=Raster(ec_r)
            ec_impact = Con(ec>8,80,Con(ec>4,50,Con(ec>2,25,10)))
            comp.append(ec_impact); cw.append(0.20)

        if wdist_r:
            wd=Raster(wdist_r)
            wd_impact = Con(wd<50,90,Con(wd<100,70,Con(wd<300,40,15)))
            comp.append(wd_impact); cw.append(0.15)

        # Build raster combination
        if len(comp) > 1:
            t=sum(cw)
            result = comp[1] * (cw[1]/t)
            for c,w in zip(comp[2:],cw[2:]): result = result + c*(w/t)
            eia = (base_impact * (cw[0]/t)) + result
        else:
            eia = base_impact

        if not isinstance(eia, float):
            eia = Con(eia>100,100,Con(eia<0,0,eia))
            eia.save(out_eia)
            dec = Con(eia<30,1,Con(eia<50,2,Con(eia<70,3,4)))
            dec.save(out_dec)
        else:
            messages.addMessage("  ⚠ تحذير: لم يتم تحديد طبقات Raster كافية لخريطة EIA مكانية")

        messages.addMessage("  ✓ خريطة EIA ودرجة الأثر محفوظة")
        arcpy.CheckInExtension("Spatial")
        messages.addMessage("  ✅ اكتمل التقييم البيئي")


# =============================================================================
# TOOL 12 — Raster LPI
# =============================================================================
class RasterLPI:
    def __init__(self):
        self.label = "12. مؤشر إنتاجية الأراضي LPI — Raster"
        self.description = (
            "المؤشر التكاملي النهائي يجمع كل طبقات التحليل السابقة\n"
            "المخرج: خريطة LPI (0-100) + خريطة الفئة + خريطة الأولوية"
        )
        self.canRunInBackground = False
        self.category = "5 - التقييم الشامل (Raster)"

    def getParameterInfo(self):
        params = []
        layers_w = [
            ("suit_r",    "خريطة الملاءمة (0-100)",          "Optional", 25),
            ("sqi_r",     "خريطة SQI (0-100)",                "Optional", 20),
            ("deg_r",     "خريطة التدهور (0-100، عكسي)",      "Optional", 20),
            ("soc_r",     "خريطة SOC (طن C/هكتار)",           "Optional", 10),
            ("erosion_r", "خريطة التعرية (طن/هكتار، عكسي)",  "Optional", 15),
            ("irr_r",     "خريطة الري (0-100)",               "Optional", 10),
        ]
        for n,l,r,_ in layers_w:
            p = arcpy.Parameter(n,l,"Input","GPRasterLayer",r); params.append(p)
        for n,_,_,wv in layers_w:
            p = arcpy.Parameter("w_"+n,"وزن {} (%)".format(n),"Input","GPLong","Optional"); p.value=wv; params.append(p)
        p = arcpy.Parameter("out_lpi","خريطة LPI (0-100)","Output","DERasterDataset","Required"); params.append(p)
        p = arcpy.Parameter("out_class","خريطة فئة LPI (1-5)","Output","DERasterDataset","Required"); params.append(p)
        return params

    def isLicensed(self): return True
    def updateParameters(self,p): return
    def updateMessages(self,p): return

    def execute(self, parameters, messages):
        if not _check_sa(messages): return
        suit_r,sqi_r,deg_r,soc_r,er_r,irr_r = [parameters[i].valueAsText for i in range(6)]
        w_s,w_sq,w_d,w_so,w_e,w_i = [parameters[i].value or v for i,v in zip(range(6,12),[25,20,20,10,15,10])]
        out_lpi = parameters[12].valueAsText
        out_cls = parameters[13].valueAsText

        messages.addMessage(_header("مؤشر إنتاجية الأراضي LPI"))

        comp=[]; comp_w=[]

        if suit_r:  comp.append(Raster(suit_r));            comp_w.append(w_s)
        if sqi_r:   comp.append(Raster(sqi_r));             comp_w.append(w_sq)
        if deg_r:   comp.append(100 - Raster(deg_r));       comp_w.append(w_d)   # inverted
        if soc_r:   # normalize SOC: assume 0-150 ton/ha range
                    s=Raster(soc_r); comp.append(Con(s/1.5>100,100,s/1.5)); comp_w.append(w_so)
        if er_r:    # normalize erosion: assume 0-100 t/ha/yr → invert
                    e=Raster(er_r); inv=Con(e>100,0,100-e); comp.append(inv); comp_w.append(w_e)
        if irr_r:   comp.append(Raster(irr_r));             comp_w.append(w_i)

        if not comp:
            messages.addErrorMessage("يجب تحديد طبقة Raster واحدة على الأقل")
            return

        t=sum(comp_w); lpi=comp[0]*(comp_w[0]/t)
        for c,w in zip(comp[1:],comp_w[1:]): lpi=lpi+c*(w/t)
        lpi=Con(lpi>100,100,Con(lpi<0,0,lpi))
        lpi.save(out_lpi)
        messages.addMessage("  ✓ خريطة LPI: {}".format(out_lpi))

        cls=Con(lpi>=85,1,Con(lpi>=70,2,Con(lpi>=55,3,Con(lpi>=40,4,5))))
        cls.save(out_cls)
        messages.addMessage("  ✓ خريطة الفئات: {}".format(out_cls))

        arcpy.CheckInExtension("Spatial")
        messages.addMessage("  ✅ اكتمل حساب LPI — المؤشر التكاملي النهائي")


# =============================================================================
# TOOL 13 — Vector Suitability (preserved from v2)
# =============================================================================
class VectorSuitability:
    def __init__(self):
        self.label = "13. ملاءمة المحاصيل — Vector (اختياري)"
        self.description = (
            "النمط الأصلي من LMIS v2 — يعمل على Polygon Feature Class\n"
            "يُوصى باستخدام الأداة 1 (Raster) للدراسات المكانية الشاملة"
        )
        self.canRunInBackground = False
        self.category = "8 - وضع Vector (اختياري)"

    def getParameterInfo(self):
        params = []
        p = arcpy.Parameter("input_layer","طبقة الوحدات الأرضية (Polygon)","Input","GPFeatureLayer","Required"); params.append(p)
        p = arcpy.Parameter("crop_type","المحصول","Input","GPString","Required")
        p.filter.type="ValueList"; p.filter.list=_crop_list(); p.value=_crop_list()[0]; params.append(p)
        for n,l in [("ph_field","حقل pH"),("texture_field","حقل نسجة التربة"),
                    ("slope_field","حقل الانحدار %"),("ec_field","حقل EC"),
                    ("depth_field","حقل عمق التربة cm")]:
            p = arcpy.Parameter(n,l,"Input","Field","Optional"); p.parameterDependencies=["input_layer"]; params.append(p)
        p = arcpy.Parameter("mean_temp","متوسط الحرارة °C","Input","GPDouble","Optional"); p.value=25.0; params.append(p)
        p = arcpy.Parameter("annual_rain","الأمطار السنوية مم","Input","GPDouble","Optional"); p.value=400.0; params.append(p)
        p = arcpy.Parameter("output_layer","طبقة المخرجات","Output","DEFeatureClass","Required"); params.append(p)
        return params

    def isLicensed(self): return True
    def updateParameters(self,p): return
    def updateMessages(self,p): return

    def execute(self, parameters, messages):
        in_lyr  = parameters[0].valueAsText
        csel    = parameters[1].valueAsText
        ph_f    = parameters[2].valueAsText
        tex_f   = parameters[3].valueAsText
        slp_f   = parameters[4].valueAsText
        ec_f    = parameters[5].valueAsText
        dep_f   = parameters[6].valueAsText
        temp    = parameters[7].value or 25.0
        rain    = parameters[8].value or 400.0
        out_lyr = parameters[9].valueAsText

        ckey = _crop_key(csel)
        cd   = _load_crops()["crops"][ckey]
        messages.addMessage(_header("ملاءمة المحاصيل — Vector", cd["name_ar"]))

        arcpy.management.CopyFeatures(in_lyr, out_lyr)
        for fn,ft,fl in [("SUIT_SCORE","DOUBLE",None),("SUIT_CLASS","TEXT",20),
                          ("SUIT_DESC","TEXT",60),("CROP_NAME","TEXT",40)]:
            try: arcpy.management.AddField(out_lyr,fn,ft,field_length=fl)
            except Exception: pass

        # Climate base scores
        def rng(v,lo,hi):
            if lo<=v<=hi: return 100
            elif v<lo: return max(0,100-(lo-v)/lo*200)
            else: return max(0,100-(v-hi)/hi*200)

        t_score = rng(temp, cd["temp_range"][0], cd["temp_range"][1])
        r_score = rng(rain, cd["rainfall_range"][0], cd["rainfall_range"][1])
        clim    = (t_score + r_score)/2.0

        read_f=[f for f in [ph_f,tex_f,slp_f,ec_f,dep_f] if f]
        upd_f=["SUIT_SCORE","SUIT_CLASS","SUIT_DESC","CROP_NAME"]+read_f

        with arcpy.da.UpdateCursor(out_lyr, upd_f) as cur:
            for row in cur:
                vals={f:row[4+i] for i,f in enumerate(read_f)}
                sc=[]; sw=[]
                if ph_f:  sc.append(rng(vals[ph_f] or 7,cd["ph_range"][0],cd["ph_range"][1]));  sw.append(0.25)
                if ec_f:  v=vals[ec_f] or 1; sc.append(max(0,100-(max(0,v-cd["ec_max"])/cd["ec_max"])*100)); sw.append(0.20)
                if slp_f: v=vals[slp_f] or 0; sc.append(max(0,100-(max(0,v-cd["slope_max"])/cd["slope_max"])*100)); sw.append(0.15)
                if dep_f: v=vals[dep_f] or 100; dm=cd["soil_depth_min"]; sc.append(100 if v>=dm else (v/dm)*100); sw.append(0.15)
                sc.append(clim); sw.append(0.25)
                total_w=sum(sw); final=sum(s*w for s,w in zip(sc,sw))/total_w if total_w>0 else 50
                cls,desc=("S1","مناسب جداً") if final>=80 else ("S2","مناسب") if final>=60 else ("S3","هامشي") if final>=40 else ("N1","غير مناسب حالياً") if final>=20 else ("N2","غير مناسب دائماً")
                row[0]=round(final,2); row[1]=cls; row[2]=desc; row[3]=cd["name_ar"]
                cur.updateRow(row)

        messages.addMessage("  ✅ اكتملت ملاءمة Vector")


# =============================================================================
# TOOL 14 — Raster to Summary Report
# =============================================================================
class RasterToReport:
    def __init__(self):
        self.label = "14. تقرير إحصائي من خرائط Raster"
        self.description = (
            "استخراج إحصاءات شاملة من أي خريطة Raster ناتجة عن LMIS\n"
            "المخرج: جدول إحصائي + تقرير HTML"
        )
        self.canRunInBackground = False
        self.category = "9 - التقارير"

    def getParameterInfo(self):
        params = []
        p = arcpy.Parameter("input_rasters","خرائط Raster المراد تقريرها","Input","GPRasterLayer","Required"); p.multiValue=True; params.append(p)
        p = arcpy.Parameter("study_area","اسم منطقة الدراسة","Input","GPString","Required"); p.value="منطقة الدراسة"; params.append(p)
        p = arcpy.Parameter("researcher","الباحث/المشروع","Input","GPString","Optional"); params.append(p)
        p = arcpy.Parameter("out_table","جدول الإحصاء المخرج","Output","DETable","Required"); params.append(p)
        p = arcpy.Parameter("out_html","ملف HTML للتقرير","Output","DEFile","Required"); p.filter.list=["html"]; params.append(p)
        return params

    def isLicensed(self): return True
    def updateParameters(self,p): return
    def updateMessages(self,p): return

    def execute(self, parameters, messages):
        rasters   = parameters[0].valueAsText.split(";")
        study     = parameters[1].valueAsText
        researcher= parameters[2].valueAsText or "LMIS System"
        out_tbl   = parameters[3].valueAsText
        out_html  = parameters[4].valueAsText

        messages.addMessage(_header("تقرير إحصائي — Raster", study))

        # Create summary table
        arcpy.management.CreateTable(os.path.dirname(out_tbl), os.path.basename(out_tbl))
        for fn,ft,fl in [("RASTER_NAME","TEXT",150),("MIN_VAL","DOUBLE",None),
                          ("MAX_VAL","DOUBLE",None),("MEAN_VAL","DOUBLE",None),
                          ("STD_VAL","DOUBLE",None)]:
            arcpy.management.AddField(out_tbl,fn,ft,field_length=fl)

        rows_data = []
        with arcpy.da.InsertCursor(out_tbl,["RASTER_NAME","MIN_VAL","MAX_VAL","MEAN_VAL","STD_VAL"]) as cur:
            for r in rasters:
                r=r.strip().strip("'")
                if not r: continue
                try:
                    mn = float(arcpy.management.GetRasterProperties(r,"MINIMUM").getOutput(0))
                    mx = float(arcpy.management.GetRasterProperties(r,"MAXIMUM").getOutput(0))
                    me = float(arcpy.management.GetRasterProperties(r,"MEAN").getOutput(0))
                    st = float(arcpy.management.GetRasterProperties(r,"STD").getOutput(0))
                    nm = os.path.basename(r)
                    cur.insertRow([nm,round(mn,3),round(mx,3),round(me,3),round(st,3)])
                    rows_data.append((nm,round(mn,3),round(mx,3),round(me,3),round(st,3)))
                    messages.addMessage("  ✓ {}".format(nm))
                except Exception as e:
                    messages.addWarningMessage("  ⚠ {}: {}".format(r,str(e)))

        # HTML report
        rows_html = "".join(
            "<tr><td><strong>{}</strong></td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(*r)
            for r in rows_data
        )
        now = datetime.datetime.now()
        html = """<!DOCTYPE html><html lang="ar" dir="rtl">
<head><meta charset="UTF-8"><title>LMIS Report</title>
<style>
body{{font-family:Arial,sans-serif;background:#f4f7f4;color:#222;direction:rtl;margin:0}}
.header{{background:linear-gradient(135deg,#1B4332,#2D6A4F);color:white;padding:40px;text-align:center}}
.header h1{{font-size:2em;margin:0 0 8px}}
.header p{{opacity:.85;margin:4px 0}}
.container{{max-width:900px;margin:30px auto;padding:0 20px}}
table{{width:100%;border-collapse:collapse;background:white;border-radius:12px;overflow:hidden;box-shadow:0 4px 15px rgba(0,0,0,.1)}}
th{{background:#1B4332;color:white;padding:12px 16px;text-align:right}}
td{{padding:11px 16px;border-bottom:1px solid #eee}}
tr:hover td{{background:#f0fdf4}}
.footer{{text-align:center;padding:20px;color:#666;font-size:.85em;margin-top:30px}}
</style></head>
<body>
<div class="header">
<h1>🌍 تقرير LMIS v3.0</h1>
<p><strong>{study}</strong> | الباحث: {res} | {date}</p>
</div>
<div class="container">
<h2 style="color:#1B4332;margin:25px 0 15px">إحصاءات طبقات Raster</h2>
<table>
<thead><tr><th>اسم الطبقة</th><th>الحد الأدنى</th><th>الحد الأقصى</th><th>المتوسط</th><th>الانحراف المعياري</th></tr></thead>
<tbody>{rows}</tbody>
</table>
</div>
<div class="footer">LMIS v3.0 — تطوير نظام د. يونس إدريس | {date}</div>
</body></html>""".format(study=study, res=researcher, date=now.strftime("%Y-%m-%d"), rows=rows_html)

        with open(out_html,"w",encoding="utf-8") as f:
            f.write(html)

        messages.addMessage("  ✓ جدول الإحصاء: {}".format(out_tbl))
        messages.addMessage("  ✓ تقرير HTML: {}".format(out_html))
        messages.addMessage("  ✅ اكتمل التقرير")
