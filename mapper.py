from lxml.builder import ElementMaker
from lxml import etree


message_maker = ElementMaker(namespace="schemas/message.xsd", nsmap={None: "schemas/message.xsd"})
def message_to_xml(message):
    xml_message = message_maker.message(
        message_maker.source(message.source),
        message_maker.userid(message.userid),
        message_maker.text(message.text),
    )
    return etree.tostring(xml_message)
    