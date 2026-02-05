'''
Django-sivustohakutoteutus: haku tekstimuotoisella hakusanalla yhtä aikaa
kaikista haun piiriin rekisteröidyistä tietomalleista.

Kussakin halutussa mallissa käytetään koristetta `@sivustohaku.Hakemisto(...)`,
jolloin malli rekisteröidään sivustohaun piiriin.

Itse haku tehdään käyttämällä funktiota `sivustohaku.Hakemisto.haku(...)`.

>>> from django.db import models
>>> from sivustohaku import Hakemisto
>>>
>>> @Hakemisto(
...   kentta='nimi__icontains',
...   hakuehto=r'.{5,}',
...   relevanssi=0.75,
... )
... class Henkilo(models.Model):
...   nimi = models.CharField(max_length=255)
...   aika = models.DateTimeField(auto_now_add=True)
...   class Meta:
...     verbose_name, verbose_name_plural = 'henkilö', 'henkilöt'
>>>
>>> async def hae_henkiloa(request):
...   'Palauta `?haku=X`-ehtoon täsmäävät henkilöt JSON-listana.'
...   async for hakutulos in Hakemisto.haku(
...     request, request.GET['haku']
...   ):
...     if hakutulos.tyyppi == 'henkilöt':
...       return JsonResponse([
...          {'nimi': henkilo.teksti, 'url': henkilo.url}
...          for henkilo in hakutulos.tietueet
...       ], safe=False)
...   return JsonResponse([], safe=False)

Tarvittaessa voidaan periyttää `Hakemisto`-luokka ja täsmentää
käyttäjäkohtaisten hakuoikeuksien määrittelyä, täydentää kullekin
hakutulokselle palautuvaa dataa tms.

>>> class Hakemisto(Hakemisto):
...   class Hakutulos(Hakemisto.Hakutulos):
...     @dataclass
...     class HaettuTietue(Hakemisto.Hakutulos.HaettuTietue):
...       aika: datetime
...
...       @classmethod
...       def tietueen_mukaan(cls, tietue: models.Model) -> Self
...         return cls(
...           teksti=str(tietue),
...           url=tietue.get_absolute_url(),
...           aika=tietue.aika
...         )
...
...   @classmethod
...   async def hakuoikeus_tietueisiin(
...     cls,
...     request: HttpRequest,
...     malli: type[models.Model]
...   ) -> models.QuerySet:
...     if await request.user.ahas_perm('sivustohaku')
...       return malli.objects.all()
...     return malli.objects.none()
>>>
>>> @Hakemisto(kentta='nimi__icontains', ...)
... class Henkilo(models.Model):
...   ...
>>>
>>> async def hae_henkiloa(request):
...   async for hakutulos in Hakemisto.haku(...): ...
'''

from collections.abc import Sequence
from dataclasses import dataclass, field
import itertools
from operator import attrgetter
import re
from typing import (
  Any,
  AsyncIterator,
  Callable,
  ClassVar,
  Optional,
  Self,
  Union,
  cast,
)

from django.db import models
from django.http import HttpRequest


@dataclass
class Hakemisto:
  ''' Yksittäiseen malliin/sarakkeeseen kohdistuva hakemisto tekstihaulle. '''

  # Malli, josta haetaan.
  malli: type[models.Model]

  # Mallin kenttä, johon haku kohdistuu.
  kentta: str = field(kw_only=True)

  # Mahdollinen kriteeri hakutermille. Ellei täyty, hakemisto ohitetaan.
  hakuehto: Optional[str | re.Pattern] = field(kw_only=True, default=None)

  # Mahdollinen muunnos hakutermille ennen hakua.
  haku_muunnos: Callable[[str], Any] = field(
    kw_only=True,
    default=lambda haku: haku,
  )

  # Montako hakuun täsmäävää tulosta mallista enintään palautuu?
  enintaan: int = field(kw_only=True, default=3)

  # Hakutulosten suhteellinen relevanssi vrt. muihin hakemistoihin.
  relevanssi: float = field(kw_only=True, default=0.0)

  # Mahdollinen muunnos Django-kyselyyn, josta tuloksia haetaan.
  kysely: Callable[[models.QuerySet], models.QuerySet] = field(
    kw_only=True,
    default=lambda qs: qs,
  )

  # Kaikki ajonaikaisesti rekisteröidyt Hakemistot luettelona.
  hakemistot: ClassVar[list[Self]] = []

  @dataclass
  class Hakutulos:
    ''' Tehtyyn hakuun täsmäävät tietueet yksittäisen Hakemiston osalta. '''
    @dataclass
    class HaettuTietue:
      ''' Tehtyyn hakuun täsmäävä, yksittäinen tietue. '''
      teksti: str         # str(tietue)
      url: Optional[str]  # tietue.get_absolute_url()

      @classmethod
      def tietueen_mukaan(cls, tietue: models.Model) -> Self:
        ''' Palauta yksittäisen, haetun tietueen data. '''
        return cls(
          teksti=str(tietue),
          url=(
            get_absolute_url()
            if (
              get_absolute_url := getattr(tietue, 'get_absolute_url', None)
            ) is not None
            else None
          ),
        )
        # def tietueen_mukaan -> Self

    relevanssi: float             # 0..1
    tietueet: list[HaettuTietue]
    tyyppi: str                   # verbose_name_plural

    @classmethod
    def tietueiden_mukaan(
      cls,
      malli: type[models.Model],
      hakemistokohtaiset_tietueet: Sequence[tuple['Hakemisto', models.Model]]
    ) -> Self:
      ''' Palauta hakutulos löydetyille tietueille. '''
      return cls(
        tyyppi=str(malli._meta.verbose_name_plural),
        tietueet=[
          cls.HaettuTietue.tietueen_mukaan(tietue)
          for tietue in {
            tietue
            for hakemisto, tietue in hakemistokohtaiset_tietueet[:max(
              hakemisto.enintaan
              for hakemisto, tietue in hakemistokohtaiset_tietueet
            )]
          }
        ],
        relevanssi=max(
          hakemisto.relevanssi
          for hakemisto, tietue in hakemistokohtaiset_tietueet
        ),
      )
      # def tietueiden_mukaan -> Self

    # class Hakutulos

  def __post_init__(self):
    ''' Alusta mahdollinen `hakuehto`; rekisteröi Hakemisto. '''
    if isinstance(self.hakuehto, str):
      self.hakuehto = re.compile(self.hakuehto)
    __class__.hakemistot.append(self)
    # def __post_init__

  def __new__(
    cls,
    malli: Optional[type[models.Model]] = None,
    **kwargs
  ) -> Union[
    type[models.Model],
    Callable[[type[models.Model]], type[models.Model]]
  ]:
    ''' Salli käyttö koristeena. '''
    if malli is not None:
      super().__new__(cls).__init__(malli, **kwargs)
      return malli

    def aseta(malli: type[models.Model]):
      cls(malli, **kwargs)
      return malli

    return aseta
    # def __new__

  async def tee_haku(
    self,
    haku: str,
    tietueet: Optional[models.QuerySet] = None
  ) -> AsyncIterator[models.Model]:
    ''' Toteuta tekstihaku Hakemistolle määritettyjen parametrien mukaan. '''
    async for tulos in self.kysely(
      self.malli.objects.all() if tietueet is None else tietueet
    ).filter(**{self.kentta: self.haku_muunnos(haku)})[:self.enintaan]:
      yield tulos
    # async def tee_haku

  @classmethod
  async def hakuoikeus_tietueisiin(
    cls,
    request: HttpRequest,
    malli: type[models.Model]
  ) -> models.QuerySet:
    '''
    Palauta ne mallin tietueet, joista käyttäjällä on oikeus hakea.

    Oletuksena pääkäyttäjällä kaikki tietueet, muilla tyhjä joukko.

    Periytettävissä tarkemman oikeustarkastelun tekemiseksi.
    '''
    if request.user.is_superuser:
      return malli.objects.all()
    return malli.objects.none()
    # async def hakuoikeus_tietueisiin

  @classmethod
  async def haku(
    cls,
    request: HttpRequest,
    haku: str,
  ) -> AsyncIterator[Hakutulos]:
    '''
    Hae hakusanalla kaikista niistä rekisteröidyistä malleista, joihin
    käyttäjällä on katseluoikeus.

    Tuotetaan yksi tulosrivi per malli, koostettuna relevanteimmat
    tulokset ensin.
    '''
    for malli, hakemistot in itertools.groupby(
      sorted(
        cls.hakemistot,
        key=attrgetter('malli.__name__'),
      ),
      key=attrgetter('malli'),
    ):
      if not (hakuoikeus_tietueisiin := await cls.hakuoikeus_tietueisiin(
        request,
        malli
      )).query.is_empty() and (
        hakemistokohtaiset_tietueet := [
          (_hakemisto, tietue)
          for _hakemisto in sorted(
            hakemistot,
            key=attrgetter('relevanssi'),
            reverse=True
          )
          if haku
          and (
            _hakemisto.hakuehto is None
            or cast(
              re.Pattern,
              _hakemisto.hakuehto
            ).match(haku) is not None
          ) and (tietueet := [
            tietue
            async for tietue in _hakemisto.tee_haku(
              haku, hakuoikeus_tietueisiin
            )
          ])
          for tietue in tietueet
        ]
      ):
        yield cls.Hakutulos.tietueiden_mukaan(
          malli,
          hakemistokohtaiset_tietueet
        )
      # for malli, hakemistot in itertools.groupby
    # async def haku

  # class Hakemisto
