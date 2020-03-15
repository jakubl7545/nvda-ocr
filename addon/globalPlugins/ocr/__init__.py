"""NVDA OCR plugin
This plugin uses Tesseract for OCR: http://code.google.com/p/tesseract-ocr/
It also uses the Python Imaging Library (PIL): http://www.pythonware.com/products/pil/
@author: James Teh <jamie@nvaccess.org>
@author: Rui Batista <ruiandrebatista@gmail.com>
@copyright: 2011-2013 NV Access Limited, Rui Batista
@license: GNU General Public License version 2.0
"""

import sys
import os
import tempfile
import subprocess
from xml.parsers import expat
from collections import namedtuple
import wx
import config
import globalPluginHandler
import gui
import api
from logHandler import log
import languageHandler
import addonHandler
addonHandler.initTranslation()
import textInfos.offsets
import ui
import locationHelper

PLUGIN_DIR = os.path.dirname(__file__)
TESSERACT_EXE = os.path.join(PLUGIN_DIR, "tesseract", "tesseract.exe")

# Pillow requires pathlib which is not bundled with NVDA
# Therefore place it in the plugin directory and add it temporarily to PYTHONPATH
sys.path.append(PLUGIN_DIR)
from .PIL import ImageGrab
from .PIL import Image
del sys.path[-1]

IMAGE_RESIZE_FACTOR = 2

OcrWord = namedtuple("OcrWord", ("offset", "left", "top"))

class HocrParser(object):

	def __init__(self, xml, leftCoordOffset, topCoordOffset):
		self.leftCoordOffset = leftCoordOffset
		self.topCoordOffset = topCoordOffset
		parser = expat.ParserCreate("utf-8")
		parser.StartElementHandler = self._startElement
		parser.EndElementHandler = self._endElement
		parser.CharacterDataHandler = self._charData
		self._textList = []
		self.textLen = 0
		self.lines = []
		self.words = []
		self._hasBlockHadContent = False
		parser.Parse(xml)
		self.text = "".join(self._textList)
		del self._textList

	def _startElement(self, tag, attrs):
		print(f"ElementStart: {tag} {attrs}")
		if tag in ("p", "div"):
			self._hasBlockHadContent = False
		elif tag == "span":
			cls = attrs["class"]
			if cls == "ocr_line":
				self.lines.append(self.textLen)
			elif cls == "ocr_word":
				# Get the coordinates from the bbox info specified in the title attribute.
				title = attrs.get("title")
				print(f"title: {title}")
				prefix, l, t, r, b = title.split(" ")
				self.words.append(OcrWord(self.textLen,
					self.leftCoordOffset + int(l) / IMAGE_RESIZE_FACTOR,
					self.topCoordOffset + int(t) / IMAGE_RESIZE_FACTOR))

	def _endElement(self, tag):
		print(f"elementEnd: {tag}")
		pass

	def _charData(self, data):
		if data.isspace():
			if not self._hasBlockHadContent:
				# Strip whitespace at the start of a block.
				return
			# All other whitespace should be collapsed to a single space.
			data = " "
			if self._textList and self._textList[-1] == data:
				return
		self._hasBlockHadContent = True
		self._textList.append(data)
		self.textLen += len(data)

class OcrTextInfo(textInfos.offsets.OffsetsTextInfo):

	def __init__(self, obj, position, parser):
		self._parser = parser
		super(OcrTextInfo, self).__init__(obj, position)

	def copy(self):
		return self.__class__(self.obj, self.bookmark, self._parser)

	def _getTextRange(self, start, end):
		return self._parser.text[start:end]

	def _getStoryLength(self):
		return self._parser.textLen

	def _getLineOffsets(self, offset):
		start = 0
		for end in self._parser.lines:
			if end > offset:
				return (start, end)
			start = end
		return (start, self._parser.textLen)

	def _getWordOffsets(self, offset):
		start = 0
		for word in self._parser.words:
			if word.offset > offset:
				return (start, word.offset)
			start = word.offset
		return (start, self._parser.textLen)

	def _getPointFromOffset(self, offset):
		for nextWord in self._parser.words:
			if nextWord.offset > offset:
				break
			word = nextWord
		else:
			# No matching word, so use the top left of the object.
			return locationHelper.Point(int(self._parser.leftCoordOffset), int(self._parser.topCoordOffset))
		return locationHelper.Point(int(word.left), int(word.top))

class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self):
		super(globalPluginHandler.GlobalPlugin, self).__init__()
		self.ocrSettingsItem = gui.mainFrame.sysTrayIcon.preferencesMenu.Append(wx.ID_ANY,
			# Translators: The name of the OCR settings item
			# in the NVDA Preferences menu.
			_("OCR settings..."))
		gui.mainFrame.sysTrayIcon.Bind(wx.EVT_MENU, self.onOCRSettings, self.ocrSettingsItem)

	def terminate(self):
		try:
			gui.mainFrame.sysTrayIcon.preferencesMenu.RemoveItem(self.ocrSettingsItem)
		except wx.PyDeadObjectError:
			pass

	def onOCRSettings(self, event):
		# Pop a dialog with available OCR languages to set
		langs = sorted(getAvailableTesseractLanguages())
		curlang = config.conf["ocr"]["language"]
		try:
			select = langs.index(curlang)
		except ValueError:
			select = langs.index('eng')
		choices = [languageHandler.getLanguageDescription(tesseractLangsToLocales[lang]) or tesseractLangsToLocales[lang] for lang in langs]
		log.debug("Available OCR languages: %s", ", ".join(choices))
		dialog = wx.SingleChoiceDialog(gui.mainFrame, _("Select OCR Language"), _("OCR Settings"), choices=choices)
		dialog.SetSelection(select)
		gui.mainFrame.prePopup()
		ret = dialog.ShowModal()
		gui.mainFrame.postPopup()
		if ret == wx.ID_OK:
			lang = langs[dialog.GetSelection()]
			config.conf["ocr"]["language"] = lang

	def script_ocrNavigatorObject(self, gesture):
		nav = api.getNavigatorObject()
		left, top, width, height = nav.location
		img = ImageGrab.grab(bbox=(left, top, left + width, top + height))
		# Tesseract copes better if we convert to black and white...
		img = img.convert(mode='L')
		# and increase the size.
		img = img.resize((width * IMAGE_RESIZE_FACTOR, height * IMAGE_RESIZE_FACTOR), Image.BICUBIC)
		baseFile = os.path.join(tempfile.gettempdir(), "nvda_ocr")
		try:
			imgFile = baseFile + ".bmp"
			img.save(imgFile)

			ui.message(_("Running OCR"))
			lang = config.conf["ocr"]["language"]
			# Hide the Tesseract window.
			si = subprocess.STARTUPINFO()
			si.dwFlags = subprocess.STARTF_USESHOWWINDOW
			si.wShowWindow = subprocess.SW_HIDE
			subprocess.check_call((TESSERACT_EXE, imgFile, baseFile, "-l", lang, "hocr"),
				startupinfo=si)
		finally:
			try:
				os.remove(imgFile)
			except OSError:
				pass
		try:
			hocrFile = baseFile + ".html"

			parser = HocrParser(open(hocrFile,encoding='utf8').read(),
				left, top)
		finally:
			try:
				os.remove(hocrFile)
			except OSError:
				pass

		# Let the user review the OCR output.
		nav.makeTextInfo = lambda position: OcrTextInfo(nav, position, parser)
		api.setReviewPosition(nav.makeTextInfo(textInfos.POSITION_FIRST))
		ui.message(_("Done"))

	__gestures = {
		"kb:NVDA+r": "ocrNavigatorObject",
	}

localesToTesseractLangs = {
"bg" : "bul",
"ca" : "cat",
"cs" : "ces",
"zh_CN" : "chi_tra",
"da" : "dan",
"de" : "deu",
"el" : "ell",
"en" : "eng",
"fi" : "fin",
"fr" : "fra",
"hu" : "hun",
"id" : "ind",
"it" : "ita",
"ja" : "jpn",
"ko" : "kor",
"lv" : "lav",
"lt" : "lit",
"nl" : "nld",
"nb_NO" : "nor",
"pl" : "pol",
"pt" : "por",
"ro" : "ron",
"ru" : "rus",
"sk" : "slk",
"sl" : "slv",
"es" : "spa",
"sr" : "srp",
"sv" : "swe",
"tg" : "tgl",
"tr" : "tur",
"uk" : "ukr",
"vi" : "vie"
}
tesseractLangsToLocales = {v : k for k, v in localesToTesseractLangs.items()}

def getAvailableTesseractLanguages():
	dataDir = os.path.join(os.path.dirname(__file__), "tesseract", "tessdata")
	dataFiles = [file for file in os.listdir(dataDir) if file.endswith('.traineddata')]
	return [os.path.splitext(file)[0] for file in dataFiles]

def getDefaultLanguage():
	lang = languageHandler.getLanguage()
	if lang not in localesToTesseractLangs and "_" in lang:
		lang = lang.split("_")[0]
	return localesToTesseractLangs.get(lang, "eng")

configspec = {
	"language" : f"string(default={getDefaultLanguage()})"
}

config.conf.spec["ocr"] = configspec
