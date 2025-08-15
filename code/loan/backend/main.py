# backend/main.py
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Dict, Any
import math

app = FastAPI(title="Taxes & Loans API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

from typing import Optional

class TaxRequest(BaseModel):
    annual_income: float = Field(..., ge=0)
    deductions: float = Field(0, ge=0)
    slabs: List[Dict[str, Optional[float]]] = Field(
        default=[
            {"upto": 250000, "rate": 0.0},
            {"upto": 500000, "rate": 0.05},
            {"upto": 1000000, "rate": 0.20},
            {"upto": None, "rate": 0.30},  # None means no limit
        ]
    )


class LoanRequest(BaseModel):
    principal: float = Field(..., gt=0)
    annual_rate_pct: float = Field(..., ge=0)
    years: float = Field(..., gt=0)
    payments_per_year: int = Field(default=12, ge=1)

class PrepayScenario(BaseModel):
    principal: float = Field(..., gt=0)
    annual_rate_pct: float = Field(..., ge=0)
    years: int = Field(..., gt=0)
    extra_monthly: float = Field(0, ge=0)
    payments_per_year: int = Field(default=12, ge=1)
    invest_rate_pct: float = Field(..., ge=0)  # expected investment return for comparison
    inflation_pct: float = Field(default=0.0, ge=0)

class QuizAnswer(BaseModel):
    answers: Dict[str, int]  # question_id -> choice_index

# ---- Tax calculation logic ----
def compute_progressive_tax(taxable_income: float, slabs: List[Dict[str, float]]):
    """
    slabs: list of dicts {"upto": float or None, "rate": decimal (0.05 for 5%)}
    returns dict with breakdown and tax_due
    """
    remaining = taxable_income
    prev_cap = 0.0
    tax = 0.0
    breakdown = []
    for slab in slabs:
        upto = slab.get("upto")  # None means infinity
        rate = float(slab.get("rate", 0.0))
        cap = upto if upto is not None else float("inf")
        band = max(0.0, min(cap - prev_cap, remaining))
        band_tax = band * rate
        breakdown.append({"band_from": prev_cap, "band_to": (cap if cap!=float("inf") else None), "taxable": round(band,2), "rate": rate, "tax": round(band_tax,2)})
        tax += band_tax
        remaining -= band
        prev_cap = cap
        if remaining <= 0:
            break
    return {"tax_due": round(tax,2), "breakdown": breakdown}

@app.post("/api/tax")
def tax_calc(req: TaxRequest):
    taxable = max(0.0, req.annual_income - req.deductions)
    result = compute_progressive_tax(taxable, req.slabs)
    result.update({
        "annual_income": req.annual_income,
        "deductions": req.deductions,
        "taxable_income": round(taxable,2),
        "effective_rate_pct": round((result["tax_due"]/req.annual_income*100) if req.annual_income>0 else 0.0, 2)
    })
    return result

# ---- Loan amortization logic ----
def amortization_schedule(principal: float, annual_rate_pct: float, years: float, payments_per_year: int = 12):
    r = annual_rate_pct/100.0
    n = int(round(years * payments_per_year))
    if r == 0:
        payment = principal / n if n>0 else principal
    else:
        rp = r / payments_per_year
        payment = principal * (rp) / (1 - (1+rp)**(-n))
    schedule = []
    balance = principal
    total_interest = 0.0
    for i in range(1, n+1):
        if r==0:
            interest = 0.0
            principal_pay = payment
        else:
            interest = balance * (r / payments_per_year)
            principal_pay = payment - interest
        # avoid tiny negative due to rounding on last payment
        if principal_pay > balance:
            principal_pay = balance
            payment = principal_pay + interest
        balance = round(balance - principal_pay, 10)
        total_interest += interest
        schedule.append({
            "period": i,
            "payment": round(payment,2),
            "principal_paid": round(principal_pay,2),
            "interest_paid": round(interest,2),
            "remaining_balance": round(balance if balance>0 else 0.0,2)
        })
        if balance <= 0:
            break
    return {
        "periods": n,
        "payment": round(payment,2),
        "total_interest": round(total_interest,2),
        "schedule": schedule
    }

@app.post("/api/loan/amortize")
def loan_amortize(req: LoanRequest):
    if req.principal <= 0 or req.years <= 0:
        raise HTTPException(status_code=400, detail="Principal and years must be > 0")
    res = amortization_schedule(req.principal, req.annual_rate_pct, req.years, req.payments_per_year)
    return {"principal": req.principal, "annual_rate_pct": req.annual_rate_pct, "years": req.years, "payments_per_year": req.payments_per_year, **res}

# ---- Prepay vs Invest scenario
@app.post("/api/loan/prepay_vs_invest")
def prepay_vs_invest(req: PrepayScenario):
    # baseline amortization without extra
    base = amortization_schedule(req.principal, req.annual_rate_pct, req.years, req.payments_per_year)
    # amortization with extra monthly (converted to payment period)
    # We'll treat extra_monthly as extra per month; if payments_per_year !=12, scale.
    scaled_extra = req.extra_monthly * (req.payments_per_year/12.0)
    # create adjusted schedule where payment = base.payment + scaled_extra and recompute amortization
    p = req.principal
    r = req.annual_rate_pct/100.0
    n_max = int(req.years * req.payments_per_year * 5)  # safety cap
    rp = r / req.payments_per_year
    payment = base["payment"] + scaled_extra
    schedule = []
    balance = p
    total_interest = 0.0
    period = 0
    while balance > 0.0001 and period < n_max:
        period += 1
        interest = balance * rp
        principal_paid = payment - interest
        if principal_paid <= 0:
            # payment too small to cover interest
            raise HTTPException(status_code=400, detail="Extra payment too small — loan won't amortize with this payment")
        if principal_paid > balance:
            principal_paid = balance
            payment = principal_paid + interest
        balance -= principal_paid
        total_interest += interest
        schedule.append({"period": period, "payment": round(payment,2), "principal_paid": round(principal_paid,2), "interest_paid": round(interest,2), "remaining_balance": round(max(balance,0),2)})
    payoff_months = period / (req.payments_per_year/12.0)  # convert to months equivalent
    # approximate value of investing the extra instead: future value of a monthly series at invest_rate_pct for original loan term
    invest_r = req.invest_rate_pct/100.0
    monthly_invest_r = invest_r/12.0
    months = int(req.years*12)
    if monthly_invest_r == 0:
        fv = req.extra_monthly * months
    else:
        fv = req.extra_monthly * ( ( (1+monthly_invest_r)**months - 1) / monthly_invest_r )
    # convert total interest saved relative to base
    interest_saved = base["total_interest"] - total_interest
    return {
        "base": base,
        "with_extra": {"payment": round(payment,2), "payoff_periods": period, "total_interest": round(total_interest,2), "schedule_sample": schedule[:6]},
        "payoff_months_equivalent": round(payoff_months,1),
        "interest_saved": round(interest_saved,2),
        "invest_future_value_of_extra": round(fv,2),
        "advice": ("Prepay if interest_saved (after tax) > expected after-tax investment return and you value guaranteed saving; invest if you prefer liquidity and higher expected returns.")
    }

# ---- Small quiz API ----
QUIZ = [
    {
        "id": "q1",
        "q": "Which of these will typically reduce your taxable income?",
        "choices": ["Standard deduction / tax-exempt contributions", "Increasing your gross salary", "Ignoring receipts"],
        "answer": 0,
        "explanation": "Contributions to certain retirement/savings and allowed deductions reduce taxable income."
    },
    {
        "id": "q2",
        "q": "If you prepay a high-interest credit card, you are:",
        "choices": ["Reducing future interest expense", "Increasing interest expense", "Decreasing credit score instantly"],
        "answer": 0,
        "explanation": "Paying down high-interest debt reduces interest charges going forward."
    },
    {
        "id": "q3",
        "q": "Loan amortization means:",
        "choices": ["Gradually paying principal + interest over time", "A one-time fee", "A tax"],
        "answer": 0,
        "explanation": "Amortization is the schedule of paying both interest and principal over time."
    }
]

@app.get("/api/quiz")
def get_quiz():
    # return questions without answers
    return [{"id": q["id"], "q": q["q"], "choices": q["choices"]} for q in QUIZ]

@app.post("/api/quiz/score")
def score_quiz(ans: QuizAnswer):
    score = 0
    feedback = []
    for q in QUIZ:
        user_choice = ans.answers.get(q["id"])
        correct = (user_choice == q["answer"])
        if correct: score += 1
        feedback.append({"id": q["id"], "correct": correct, "explanation": q["explanation"], "correct_choice_index": q["answer"]})
    return {"score": score, "out_of": len(QUIZ), "feedback": feedback}

# ---- Root ----
@app.get("/")
def root():
    return {"status": "Taxes & Loans API running"}

