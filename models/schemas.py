from pydantic import BaseModel, field_validator
from typing import Optional
from datetime import datetime
from enum import Enum


"""
Agent 1 (Sharp move investigation)
    - When sudden price movement is detected, automatically triggers investigation
        for the cause, and how it can affect your position currently and the future

Agent 2 (Cross-portfolio reasoning)
    - After each update cycle
    - Looks across all holdings simultaneously - finds sector trends, correlations and risk we wouldn't see stock by stock
    !! What does my portfolio look like as a whole as of now?
    
Agent 3 (Motive reassessment)
    - Per week
    - Checks whether the user's motive for entering a position still holds given what's actually happened since
    !! Does your motive for being in this trade make sense at this point of time?

Agent 4 (Hypothesis gen)
    - determines its own next check-in cadence based on what is found
    - scans each stock's price data without any questions
    - if something structurally is found in the chart (via technicals),
        speculatively search online for early confirming / denying signals
    - Flags anything worth attention before it becomes obvious
    
    
|| Agents 1-3: reactive search (something happened, find why)
|| Agent 4: speculative search (seems like something will happen, find early proof)
"""



# enums -----

class Motive(str, Enum):
    HOLDING = "holding"     # long term hold for profit
    SHORT_TERM = "short-term" # short term profit
    WATCHING = "watching"   # tracked ticker we dont have position in yet


class UpdateInterval(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"


class EventType(str, Enum):
    SCHEDULED_UPDATE = "scheduled_update"
    SHARP_MOVE = "sharp_move"
    MOTIVE_CHECK = "motive_check" # eg; holding for short term and drops --> check if worth exiting soon or switch to long-term?
    HYPOTHESIS_SCAN = "hypothesis_scan" # periodically check for anything worth flagging in the more recent 150 datapoints we have (per stock)


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class AlertType(str, Enum):
    SHARP_MOVE = "sharp_move" # (1)
    MOTIVE_FLAG = "motive_flag" # (3) if stated motive for stock seems unrealistic at the moment
    HYPOTHESIS = "hypothesis" # (4) agent finds something interesting in the 150 datapoints
    CROSS_PORTFOLIO = "cross_portfolio" # (2) agent finds correlations, sector drawdowns, combined risk for all holdings
    SCHEDULED = "scheduled"



# core data ------

class OHLCVPoint(BaseModel):
    ticker: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    @field_validator("open", "high", "low", "close", "volume")
    @classmethod
    def must_be_positive(cls, v: float) -> float:
        if v < 0:
            raise ValueError("Price and volume values must be non-negative")
        return v
    

class Ticker(BaseModel):
    ticker: str
    last_fetched: Optional[datetime] = None
    next_fetch_time: Optional[datetime] = None
    current_price: Optional[float] = None


class TickerRegistry(BaseModel):
    ticker: str
    subscribers: list["Subscription"] = []
    current_price: Optional[float] = None
    last_fetched: Optional[datetime] = None

    def add_subscriber(self, sub: "Subscription") -> None:
        self.subscribers = [s for s in self.subscribers if s.user_id != sub.user_id]
        self.subscribers.append(sub)

    def remove_subscriber(self, user_id: str) -> None:
        self.subscribers = [s for s in self.subscribers if s.user_id != user_id]

    def get_subscriber(self, user_id: str) -> Optional["Subscription"]:
        return next((s for s in self.subscribers if s.user_id == user_id), None)

    @property
    def subscriber_count(self) -> int:
        return len(self.subscribers)

    @property
    def is_active(self) -> bool:
        return self.subscriber_count > 0



# user subscription --------

class User(BaseModel):
    user_id: str
    whatsapp_number: str
    email: Optional[str] = None
    created_at: Optional[datetime] = None
    preferences: Optional[dict] = {}


class Subscription(BaseModel):
    user_id: str
    ticker: str
    avg_price: float
    shares: float
    motive: Motive
    update_interval: UpdateInterval
    sharp_move_threshold: float = 0.050  # default 5.0%

    @field_validator("avg_price", "shares")
    @classmethod
    def must_be_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("avg_price and shares must be greater than zero")
        return v

    @field_validator("sharp_move_threshold")
    @classmethod
    def threshold_must_be_valid(cls, v: float) -> float:
        if not (0.001 <= v <= 0.5):
            # marginal benefit of anything above 50% does not seem worth
            raise ValueError("sharp_move_threshold must be between 0.1% and 50%")
        return v

    @property
    def position_value(self) -> float:
        return self.avg_price * self.shares



# agent input -----

class AgentContext(BaseModel): # agent 1, 3, 4
    ticker: str
    datapoints: list[OHLCVPoint]
    subscription: Subscription
    event_type: EventType
    current_price: float
    unrealized_pnl: float
    unrealized_pnl_pct: float

    @property
    def is_profitable(self) -> bool:
        return self.unrealized_pnl > 0


class PortfolioContext(BaseModel): # agent 2
    """Has all the user positions and recent agent outputs"""
    user_id: str
    positions: list[AgentContext]
    latest_outputs: list[AgentOutput] = []
    timestamp: datetime = datetime.now()

    @property
    def tickers(self) -> list[str]:
        return [p.ticker for p in self.positions]

    @property
    def total_portfolio_value(self) -> float:
        return sum(p.subscription.position_value for p in self.positions)

    @property
    def total_unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self.positions)
    


# agent output -----

class AgentOutput(BaseModel):
    ticker: str
    user_id: str
    event_type: EventType
    summary: str
    recommendation: str
    confidence: Confidence
    timestamp: datetime = datetime.now()
    price_at_update: Optional[float] = None
    searched_web: bool = False

class CrossPortfolioOutput(BaseModel):
    user_id: str
    timestamp: datetime = datetime.now()
    summary: str
    correlations_flagged: Optional[list[str]] = []
    tickers_analyzed: list[str] = []


class HypothesisOutput(BaseModel):
    ticker: str
    user_id: str
    summary: Optional[str] = None          # None if nothing flagged
    flagged: bool = False
    confidence: Confidence
    recommended_next_scan_days: int        # Claude decides this
    timestamp: datetime = datetime.now()

# notifications ------

class Alert(BaseModel):
    user_id: str
    ticker: str
    timestamp: datetime = datetime.now()
    alert_type: AlertType
    message: str
    trigger_details: Optional[dict] = {}


class WhatsAppMessage(BaseModel):
    to: str          # whatsapp number e.g. "whatsapp:+1234567890"
    body: str
    alert_type: AlertType
    ticker: Optional[str] = None
    user_id: Optional[str] = None