import logging
import sleekcomponent

if __name__ == '__main__':
    logging.basicConfig(level=5, format='%(levelname)-8s %(message)s')
    c = sleekcomponent.SleekComponent()
    c.connect()
    c.process()
